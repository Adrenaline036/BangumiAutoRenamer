"""Adapt fresh process observations, never repair arbitrary revision drafts."""
from dataclasses import replace

from .decision_state import DecisionStateStore
from .projection_authority import RecognitionDecisionInput
from .source_basis import InitialSourceBasis


def adapt_new_recognition_observation(state_store, recognition, draft):
    if type(recognition) is not RecognitionDecisionInput or recognition.task_id != draft.task_id:
        raise ValueError('新识别观察的任务归属不一致')
    previous = state_store.load_optional(draft.task_id)
    if previous is not None and previous.task_id != draft.task_id:
        raise ValueError('新识别观察与当前Decision任务归属不一致')
    fingerprint = DecisionStateStore.fingerprint(previous) if previous is not None else ''
    if (recognition.expected_previous_fingerprint is not None
            and recognition.expected_previous_fingerprint != fingerprint):
        raise ValueError('新识别观察对应的当前记录已变化')
    metadata = draft.to_dict()['metadata']
    expected = {}
    if previous is not None and previous.content_policy is not None:
        expected['content_policy'] = previous.content_policy.value
    if previous is not None and previous.initial_source_basis is not None:
        expected['initial_source_basis'] = previous.initial_source_basis.to_dict()
    for key in ('content_policy', 'initial_source_basis'):
        if key in metadata:
            if key not in expected or metadata[key] != expected[key]:
                raise ValueError(f'新识别观察的 {key} 与冻结记录冲突')
        elif key in expected:
            metadata[key] = expected[key]
    # The receipt is built only by canonical projection. A supplied derived
    # hint is checked, not used as a substitute for the typed source basis.
    if 'initial_source_basis_fingerprint' in metadata:
        basis = previous.initial_source_basis if previous is not None else None
        if type(basis) is not InitialSourceBasis or metadata['initial_source_basis_fingerprint'] != basis.fingerprint:
            raise ValueError('新识别观察的来源指纹与冻结记录冲突')
    return replace(draft, metadata=metadata), replace(
        recognition, expected_previous_fingerprint=fingerprint)

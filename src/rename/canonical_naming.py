"""Frozen title authority and Windows-portable media leaf construction.

Provider observations are resolved once before planning.  Downstream Plan,
review, execution, and recovery code consume the serialized decision and may
recompute pure values, but must not contact a Provider or infer another title.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Iterable, Mapping, Optional
import unicodedata


TITLE_POLICY_VERSION = "bar-title-authority-v2"
LEAF_POLICY_VERSION = "bar-windows-portable-leaf-v1"
MAX_LEAF_UTF16_UNITS = 240

_INVALID_WINDOWS_CHARS = {
    "<": "＜",
    ">": "＞",
    ":": "：",
    '"': "＂",
    "/": "／",
    "\\": "＼",
    "|": "｜",
    "?": "？",
    "*": "＊",
}
_RESERVED_WINDOWS_BASENAMES = frozenset({
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
})
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_FROZEN_WORK_PROJECTION = "work:frozen_projection"


class CanonicalNamingError(ValueError):
    """A title or leaf cannot be represented by the frozen policy."""


class TitleAuthorityConflict(CanonicalNamingError):
    """Provider identity/evidence has more than one authoritative outcome."""


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _identity(value: Mapping[str, object]) -> dict[str, str]:
    identity = {
        "namespace": str(value.get("namespace") or "").strip().casefold(),
        "entity_type": str(value.get("entity_type") or "").strip().casefold(),
        "provider_id": str(value.get("provider_id") or "").strip(),
    }
    if not all(identity.values()):
        raise TitleAuthorityConflict("作品资料缺少稳定的来源、类型或条目 ID")
    return identity


def _identity_key(value: Mapping[str, object]) -> tuple[str, str, str]:
    identity = _identity(value)
    return (
        identity["namespace"],
        identity["entity_type"],
        identity["provider_id"],
    )


def _first_text(value: Mapping[str, object], names: Iterable[str]) -> str:
    for name in names:
        text = str(value.get(name) or "").strip()
        if text:
            return text
    return ""


def _ordered_aliases(values: Iterable[str], display_title: str) -> tuple[str, ...]:
    by_key: dict[str, str] = {}
    display_key = unicodedata.normalize("NFC", display_title).casefold()
    for raw in values:
        value = unicodedata.normalize("NFC", str(raw or "").strip())
        key = value.casefold()
        if value and key != display_key:
            by_key.setdefault(key, value)
    return tuple(by_key[key] for key in sorted(by_key))


def _same_title(left: str, right: str) -> bool:
    """Compare frozen provider titles without using language heuristics."""

    return (
        unicodedata.normalize("NFC", str(left).strip()).casefold()
        == unicodedata.normalize("NFC", str(right).strip()).casefold()
    )


@dataclass(frozen=True)
class CanonicalNamingDecision:
    display_title: str
    title_source: str
    aliases: tuple[str, ...]
    original_title: Optional[str]
    primary_provider_identity: Mapping[str, str]
    evidence: tuple[str, ...]
    title_policy_version: str = TITLE_POLICY_VERSION
    leaf_policy_version: str = LEAF_POLICY_VERSION
    decision_fingerprint: str = ""

    def __post_init__(self) -> None:
        display = unicodedata.normalize("NFC", str(self.display_title).strip())
        if not display:
            raise CanonicalNamingError("作品标题权威为空，无法生成目录")
        identity = _identity(self.primary_provider_identity)
        aliases = _ordered_aliases(self.aliases, display)
        evidence = tuple(str(item) for item in self.evidence if str(item).strip())
        original = (
            unicodedata.normalize("NFC", str(self.original_title).strip())
            if self.original_title else None
        )
        object.__setattr__(self, "display_title", display)
        object.__setattr__(self, "primary_provider_identity", identity)
        object.__setattr__(self, "aliases", aliases)
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "original_title", original)
        payload = self._fingerprint_payload()
        expected = hashlib.sha256(_canonical_json(payload)).hexdigest()
        supplied = str(self.decision_fingerprint or "").casefold()
        if supplied and supplied != expected:
            raise CanonicalNamingError("命名决定 fingerprint 不匹配")
        object.__setattr__(self, "decision_fingerprint", expected)

    def _fingerprint_payload(self) -> dict[str, object]:
        return {
            "display_title": self.display_title,
            "title_source": self.title_source,
            "aliases": list(self.aliases),
            "original_title": self.original_title,
            "primary_provider_identity": dict(self.primary_provider_identity),
            "evidence": list(self.evidence),
            "title_policy_version": self.title_policy_version,
            "leaf_policy_version": self.leaf_policy_version,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            **self._fingerprint_payload(),
            "decision_fingerprint": self.decision_fingerprint,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "CanonicalNamingDecision":
        identity = value.get("primary_provider_identity")
        aliases = value.get("aliases", ())
        evidence = value.get("evidence", ())
        if not isinstance(identity, Mapping):
            raise CanonicalNamingError("命名决定缺少作品身份")
        if not isinstance(aliases, (list, tuple)) or not all(
            isinstance(item, str) for item in aliases
        ):
            raise CanonicalNamingError("命名决定 aliases 无效")
        if not isinstance(evidence, (list, tuple)) or not all(
            isinstance(item, str) for item in evidence
        ):
            raise CanonicalNamingError("命名决定 evidence 无效")
        if value.get("title_policy_version") != TITLE_POLICY_VERSION:
            raise CanonicalNamingError("标题权威策略版本过旧，请重新生成整理计划")
        if value.get("leaf_policy_version") != LEAF_POLICY_VERSION:
            raise CanonicalNamingError("路径字符策略版本过旧，请重新生成整理计划")
        return cls(
            display_title=str(value.get("display_title") or ""),
            title_source=str(value.get("title_source") or ""),
            aliases=tuple(aliases),
            original_title=(
                str(value.get("original_title"))
                if value.get("original_title") is not None else None
            ),
            primary_provider_identity=dict(identity),
            evidence=tuple(evidence),
            title_policy_version=str(value.get("title_policy_version") or ""),
            leaf_policy_version=str(value.get("leaf_policy_version") or ""),
            decision_fingerprint=str(value.get("decision_fingerprint") or ""),
        )


def resolve_title_authority(
    *,
    selection: Mapping[str, object],
    mapped_candidates: Iterable[Mapping[str, object]] = (),
    primary_provider_identity: Optional[Mapping[str, object]] = None,
) -> CanonicalNamingDecision:
    """Resolve one display title from the explicit selection path and identity."""

    selected_identity = _identity(selection)
    primary_identity = (
        _identity(primary_provider_identity)
        if primary_provider_identity is not None else selected_identity
    )
    candidates = tuple(dict(item) for item in mapped_candidates)
    if candidates:
        identities = {_identity_key(item) for item in candidates}
        primary_key = _identity_key(primary_identity)
        if primary_provider_identity is None and len(identities) > 1:
            raise TitleAuthorityConflict("作品映射存在多个不同条目，不能自动决定标题")
        matched = tuple(
            item for item in candidates if _identity_key(item) == primary_key
        )
        if primary_provider_identity is not None:
            if not matched:
                raise TitleAuthorityConflict("标题证据与最终作品身份不一致")
            candidates = matched
    elif (
        primary_provider_identity is not None
        and _identity_key(selected_identity) != _identity_key(primary_identity)
    ):
        raise TitleAuthorityConflict("最终作品身份缺少对应的冻结标题证据")

    namespace = selected_identity["namespace"]
    selected_localized = _first_text(
        selection, ("localized_title", "name_cn", "title", "name")
    )
    selected_original = _first_text(
        selection, ("original_title", "original_name")
    )
    is_frozen_work_projection = (
        selection.get("_bar_frozen_work_projection") is True
    )
    mapped_localized = {
        _first_text(item, ("localized_title", "name_cn", "title", "name"))
        for item in candidates
    }
    mapped_localized.discard("")
    mapped_original = {
        _first_text(item, ("original_title", "original_name"))
        for item in candidates
    }
    mapped_original.discard("")

    decision_original = selected_original
    if namespace == "bangumi":
        if len(mapped_localized) > 1 or len(mapped_original) > 1:
            raise TitleAuthorityConflict("同一作品存在多个同级标题证据")
        tmdb_localized = next(iter(mapped_localized), "")
        tmdb_original = next(iter(mapped_original), "")
        if tmdb_original:
            decision_original = tmdb_original
        if (
            tmdb_localized
            and tmdb_original
            and not _same_title(tmdb_localized, tmdb_original)
        ):
            display_title = tmdb_localized
            title_source = "tmdb:zh-CN"
        elif selected_localized:
            # Missing/unchanged TMDB localization cannot be promoted as a
            # Chinese short title.  The frozen Bangumi name_cn is the only
            # conservative N1 fallback; ambiguous language inference is left
            # to the later user-confirmed series-title authority.
            display_title = selected_localized
            title_source = "bangumi:name_cn"
        else:
            raise TitleAuthorityConflict("作品缺少可确认的中文标题")
    elif namespace == "tmdb":
        if _identity_key(selected_identity) != _identity_key(primary_identity):
            raise TitleAuthorityConflict("所选 TMDB 条目与最终作品身份不一致")
        if (
            selected_localized
            and selected_original
            and not _same_title(selected_localized, selected_original)
        ):
            display_title = selected_localized
            title_source = "tmdb:zh-CN"
        elif (
            selected_localized
            and not selected_original
            and is_frozen_work_projection
        ):
            # This is not Provider locale evidence.  It is an already-frozen
            # Work title projected through the one shared resolver so legacy
            # completion remains stable without inventing tmdb:zh-CN proof.
            display_title = selected_localized
            title_source = _FROZEN_WORK_PROJECTION
        else:
            raise TitleAuthorityConflict("TMDB 条目缺少可确认的中文标题")
    else:
        if not selected_localized and not selected_original:
            raise TitleAuthorityConflict("所选作品缺少可用标题")
        display_title = selected_localized or selected_original
        title_source = f"{namespace}:selected"

    aliases = _ordered_aliases(
        (
            selected_localized,
            selected_original,
            *sorted(mapped_localized, key=str.casefold),
            *sorted(mapped_original, key=str.casefold),
        ),
        display_title,
    )
    return CanonicalNamingDecision(
        display_title=display_title,
        title_source=title_source,
        aliases=aliases,
        original_title=decision_original or (aliases[0] if aliases else None),
        primary_provider_identity=primary_identity,
        evidence=(
            f"selection:{selected_identity['namespace']}/"
            f"{selected_identity['entity_type']}/{selected_identity['provider_id']}",
            f"primary:{primary_identity['namespace']}/"
            f"{primary_identity['entity_type']}/{primary_identity['provider_id']}",
            *((_FROZEN_WORK_PROJECTION,) if is_frozen_work_projection else ()),
        ),
    )


def select_bangumi_work_title_owner(
    *,
    subjects: Iterable[Mapping[str, object]],
    season_assignments: Mapping[str, object],
) -> dict[str, object]:
    """Choose one stable Bangumi title owner for a multi-season TMDB work.

    Split-cour subjects can legitimately share one TMDB season.  The earliest
    frozen air date breaks that tie; missing/equal evidence remains ambiguous
    and fails closed instead of depending on traversal or arrival order.
    """

    assignments: dict[str, int] = {}
    for raw_subject_id, raw_season in season_assignments.items():
        if not isinstance(raw_season, int) or isinstance(raw_season, bool):
            raise TitleAuthorityConflict("季度标题证据包含无效季度编号")
        assignments[str(raw_subject_id)] = raw_season
    if not assignments:
        raise TitleAuthorityConflict("季度标题证据为空")
    earliest_season = min(assignments.values())
    owner_ids = {
        subject_id
        for subject_id, season_number in assignments.items()
        if season_number == earliest_season
    }
    rows = tuple(
        dict(subject)
        for subject in subjects
        if str(subject.get("provider_id") or subject.get("id") or "")
        in owner_ids
    )
    if len(rows) != len(owner_ids):
        raise TitleAuthorityConflict("同一作品缺少首季度标题证据")
    if len(rows) == 1:
        return rows[0]
    dated_rows = []
    for row in rows:
        raw_date = str(row.get("air_date") or row.get("date") or "").strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
            dated_rows.append((raw_date, row))
    if not dated_rows:
        raise TitleAuthorityConflict("同一作品存在多个首季度标题，且缺少发布日期")
    earliest_date = min(item[0] for item in dated_rows)
    earliest_rows = tuple(row for date, row in dated_rows if date == earliest_date)
    if len(earliest_rows) != 1:
        raise TitleAuthorityConflict("同一作品存在多个同日首季度标题，无法决定目录名")
    return earliest_rows[0]


def decision_from_work_identity(work: object) -> CanonicalNamingDecision:
    provider = getattr(work, "primary_provider", None)
    if provider is None:
        raise TitleAuthorityConflict("整理计划缺少作品资料身份")
    provider_mapping = (
        provider.to_dict() if callable(getattr(provider, "to_dict", None)) else {}
    )
    title = str(getattr(work, "title", "") or provider_mapping.get("title") or "")
    selection = {
        **provider_mapping,
        "localized_title": title,
        "_bar_frozen_work_projection": True,
    }
    aliases = tuple(provider_mapping.get("aliases") or ())
    mapped = tuple({
        **provider_mapping,
        "localized_title": alias,
    } for alias in aliases)
    return resolve_title_authority(
        selection=selection,
        mapped_candidates=mapped,
        primary_provider_identity=provider_mapping,
    )


def canonicalize_media_leaf(value: str) -> str:
    """Return one NFC Windows-portable leaf; never operate on a full path."""

    normalized = unicodedata.normalize("NFC", str(value))
    cleaned = "".join(
        "_" if ord(char) < 32 else _INVALID_WINDOWS_CHARS.get(char, char)
        for char in normalized
    )
    cleaned = " ".join(cleaned.strip(" .").split())
    if cleaned in {"", ".", ".."}:
        raise CanonicalNamingError("目录名清理后为空或仅包含点号")
    cleaned = _truncate_utf16(cleaned, MAX_LEAF_UTF16_UNITS).rstrip(" .")
    if not cleaned:
        raise CanonicalNamingError("目录名超过长度限制且无法安全截断")
    basename = cleaned.split(".", 1)[0].casefold()
    if basename in _RESERVED_WINDOWS_BASENAMES:
        raise CanonicalNamingError("目录名属于 Windows 保留设备名")
    return cleaned


def _truncate_utf16(value: str, limit: int) -> str:
    units = 0
    result = []
    for char in value:
        char_units = len(char.encode("utf-16-le")) // 2
        if units + char_units > limit:
            break
        result.append(char)
        units += char_units
    return "".join(result)


def windows_collision_key(value: str) -> str:
    return unicodedata.normalize("NFC", canonicalize_media_leaf(value)).casefold()


def deterministic_collision_suffix(identity: Mapping[str, object]) -> str:
    digest = hashlib.sha256(_canonical_json(_identity(identity))).hexdigest()
    return f" [同名-{digest[:12]}]"


def resolve_fresh_collision_root(
    *,
    generated_root: Path,
    decision: CanonicalNamingDecision | Mapping[str, object],
    registered_roots: Iterable[Path] = (),
) -> Path:
    """Disambiguate a fresh root without granting an unregistered path authority."""

    resolved = (
        decision
        if isinstance(decision, CanonicalNamingDecision)
        else read_naming_decision(decision)
    )
    generated_root = Path(generated_root)
    registered = tuple(Path(path) for path in registered_roots)
    base_key = windows_collision_key(generated_root.name)
    registered_collision = any(
        windows_collision_key(path.name) == base_key for path in registered
    )
    if not registered_collision:
        return generated_root
    suffix = deterministic_collision_suffix(
        resolved.primary_provider_identity
    )
    suffix_units = len(suffix.encode("utf-16-le")) // 2
    base_budget = MAX_LEAF_UTF16_UNITS - suffix_units
    if base_budget <= 0:
        raise CanonicalNamingError("同名消歧后缀超过 Windows 目录名长度限制")
    base = _truncate_utf16(generated_root.name, base_budget).rstrip(" .")
    if not base:
        raise CanonicalNamingError("同名目录无法生成稳定的消歧名称")
    candidate = generated_root.with_name(f"{base}{suffix}")
    candidate_key = windows_collision_key(candidate.name)
    if candidate.exists() or any(
        windows_collision_key(path.name) == candidate_key for path in registered
    ):
        # This includes the frozen 12-hex truncation collision case.  Never
        # fall back to arrival-order counters.
        raise CanonicalNamingError("确定性同名目录仍发生冲突，请人工核对作品登记")
    return candidate


def build_canonical_work_root(
    *,
    resolved_target_root: Path,
    decision: CanonicalNamingDecision | Mapping[str, object],
    year: Optional[int],
) -> Path:
    resolved = (
        decision
        if isinstance(decision, CanonicalNamingDecision)
        else CanonicalNamingDecision.from_dict(decision)
    )
    if year is not None and (
        not isinstance(year, int) or isinstance(year, bool) or year < 0
    ):
        raise CanonicalNamingError("作品年份无效")
    raw_leaf = (
        f"{resolved.display_title} ({year})"
        if year is not None else resolved.display_title
    )
    return Path(resolved_target_root) / canonicalize_media_leaf(raw_leaf)


def is_portable_media_leaf(value: str) -> bool:
    """Validate safety without requiring legacy whitespace canonicalization."""

    text = str(value)
    if not text or text in {".", ".."} or text != unicodedata.normalize("NFC", text):
        return False
    if text[-1:] in {" ", "."}:
        return False
    if any(ord(char) < 32 or char in _INVALID_WINDOWS_CHARS for char in text):
        return False
    if len(text.encode("utf-16-le")) // 2 > MAX_LEAF_UTF16_UNITS:
        return False
    return text.split(".", 1)[0].casefold() not in _RESERVED_WINDOWS_BASENAMES


def read_naming_decision(value: object) -> CanonicalNamingDecision:
    if not isinstance(value, Mapping):
        raise CanonicalNamingError("整理计划缺少冻结命名决定")
    return CanonicalNamingDecision.from_dict(value)

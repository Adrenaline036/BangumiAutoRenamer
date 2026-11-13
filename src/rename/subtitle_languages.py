"""人工字幕语言选择、规范代码及可见标题的共用规则。"""
import re
import unicodedata

# ISO 639-1 的语言代码；中文文字形式在语言代码之外单独保留。
LANGUAGES = dict(item.split('=', 1) for item in '''
aa=阿法尔语 ab=阿布哈兹语 ae=阿维斯陀语 af=南非语 ak=阿坎语 am=阿姆哈拉语 an=阿拉贡语 ar=阿拉伯语
as=阿萨姆语 av=阿瓦尔语 ay=艾马拉语 az=阿塞拜疆语 ba=巴什基尔语 be=白俄罗斯语 bg=保加利亚语 bh=比哈尔语
bi=比斯拉马语 bm=班巴拉语 bn=孟加拉语 bo=藏语 br=布列塔尼语 bs=波斯尼亚语 ca=加泰罗尼亚语 ce=车臣语
ch=查莫罗语 co=科西嘉语 cr=克里语 cs=捷克语 cu=教会斯拉夫语 cv=楚瓦什语 cy=威尔士语 da=丹麦语
de=德语 dv=迪维希语 dz=宗喀语 ee=埃维语 el=希腊语 en=英语 eo=世界语 es=西班牙语 et=爱沙尼亚语
eu=巴斯克语 fa=波斯语 ff=富拉语 fi=芬兰语 fj=斐济语 fo=法罗语 fr=法语 fy=西弗里斯语 ga=爱尔兰语
gd=苏格兰盖尔语 gl=加利西亚语 gn=瓜拉尼语 gu=古吉拉特语 gv=马恩语 ha=豪萨语 he=希伯来语 hi=印地语
ho=希里莫图语 hr=克罗地亚语 ht=海地克里奥尔语 hu=匈牙利语 hy=亚美尼亚语 hz=赫雷罗语 ia=国际语
id=印度尼西亚语 ie=西方国际语 ig=伊博语 ii=四川彝语 ik=伊努皮克语 io=伊多语 is=冰岛语 it=意大利语
iu=因纽特语 ja=日语 jv=爪哇语 ka=格鲁吉亚语 kg=刚果语 ki=基库尤语 kj=宽亚玛语 kk=哈萨克语
kl=格陵兰语 km=高棉语 kn=卡纳达语 ko=韩语 kr=卡努里语 ks=克什米尔语 ku=库尔德语 kv=科米语
kw=康沃尔语 ky=吉尔吉斯语 la=拉丁语 lb=卢森堡语 lg=卢干达语 li=林堡语 ln=林加拉语 lo=老挝语
lt=立陶宛语 lu=卢巴加丹加语 lv=拉脱维亚语 mg=马拉加斯语 mh=马绍尔语 mi=毛利语 mk=马其顿语
ml=马拉雅拉姆语 mn=蒙古语 mr=马拉地语 ms=马来语 mt=马耳他语 my=缅甸语 na=瑙鲁语 nb=挪威书面语
nd=北恩德贝莱语 ne=尼泊尔语 ng=恩敦加语 nl=荷兰语 nn=新挪威语 no=挪威语 nr=南恩德贝莱语 nv=纳瓦霍语
ny=齐切瓦语 oc=奥克语 oj=奥吉布瓦语 om=奥罗莫语 or=奥里亚语 os=奥塞梯语 pa=旁遮普语 pi=巴利语
pl=波兰语 ps=普什图语 pt=葡萄牙语 qu=克丘亚语 rm=罗曼什语 rn=基隆迪语 ro=罗马尼亚语 ru=俄语
rw=卢旺达语 sa=梵语 sc=撒丁语 sd=信德语 se=北萨米语 sg=桑戈语 si=僧伽罗语 sk=斯洛伐克语
sl=斯洛文尼亚语 sm=萨摩亚语 sn=绍纳语 so=索马里语 sq=阿尔巴尼亚语 sr=塞尔维亚语 ss=斯瓦蒂语
st=南索托语 su=巽他语 sv=瑞典语 sw=斯瓦希里语 ta=泰米尔语 te=泰卢固语 tg=塔吉克语 th=泰语
ti=提格里尼亚语 tk=土库曼语 tl=他加禄语 tn=茨瓦纳语 to=汤加语 tr=土耳其语 ts=聪加语 tt=鞑靼语
tw=特维语 ty=塔希提语 ug=维吾尔语 uk=乌克兰语 ur=乌尔都语 uz=乌兹别克语 ve=文达语 vi=越南语
vo=沃拉普克语 wa=瓦隆语 wo=沃洛夫语 xh=科萨语 yi=意第绪语 yo=约鲁巴语 za=壮语 zh=中文 zu=祖鲁语
'''.split())
COMMON = {'zh-Hans': '简体中文', 'zh-Hant': '繁体中文', 'zh': '中文（不区分简繁）',
          'ja': '日语', 'en': '英语', 'ko': '韩语'}
ALIASES = {'eng': 'en', 'jpn': 'ja', 'kor': 'ko', 'zho': 'zh', 'chi': 'zh',
           'fra': 'fr', 'fre': 'fr', 'deu': 'de', 'ger': 'de', 'spa': 'es',
           'rus': 'ru', 'por': 'pt', 'ita': 'it', 'hin': 'hi', 'tha': 'th', 'vie': 'vi'}


def language_key(value):
    raw = unicodedata.normalize('NFKC', str(value or '')).strip().casefold()
    if raw in ('zh-hans', 'zh-hant'):
        return 'zh-' + raw[3:].title()
    raw = ALIASES.get(raw, raw)
    if raw not in LANGUAGES:
        raise ValueError('请选择语言或填写有效的两字母语言代码，例如 fr、de')
    return raw


def language_name(value):
    key = language_key(value)
    return COMMON.get(key, LANGUAGES.get(key, key))


def language_fields(value):
    key = language_key(value)
    return ('zh', key[3:]) if key.startswith('zh-') else (key, None)


def format_language(language, script=None):
    key = language_key(language)
    if key != language or (script is not None and (key != 'zh' or script not in ('Hans', 'Hant'))):
        raise ValueError('字幕语言和文字形式不匹配')
    return f'zh-{script}' if script else key


def multilingual_title(main, additional=()):
    keys = (language_key(main), *(language_key(v) for v in additional))
    if len(set(keys)) != len(keys):
        raise ValueError('主语言和附加语言不能重复')
    if len(keys) == 1:
        return ''
    if set(keys) == {'zh-Hans', 'zh-Hant'}:
        return '简繁双字幕'
    short = {'zh-Hans': '简', 'zh-Hant': '繁', 'zh': '中', 'ja': '日', 'en': '英', 'ko': '韩'}
    if len(keys) == 2:
        return ''.join(short.get(k, language_name(k)) for k in keys) + '双语'
    return '、'.join({'zh-Hans': '简中', 'zh-Hant': '繁中'}.get(k, language_name(k)) for k in keys) + ({3: '三语', 4: '四语'}.get(len(keys), f'{len(keys)}语'))


def language_shaped_token(value):
    """仅在人工替换语言时消费明确语言片段，不猜任意标题。"""
    value = unicodedata.normalize('NFKC', str(value)).casefold().strip(' []【】()（）')
    from .subtitle_labels import parse_subtitle_label, TokenRole
    tokens = parse_subtitle_label(value).tokens
    if tokens and all(t.role in (TokenRole.LANGUAGE, TokenRole.COMPOSITE_LANGUAGE,
                                 TokenRole.AMBIGUOUS_LANGUAGE) for t in tokens):
        return True
    parts = re.split(r'[-_&+]', value)
    atoms = {'zh', 'sc', 'tc', 'chs', 'cht', 'hans', 'hant', 'jp', 'ja', 'jpn',
             'en', 'eng', 'ko', 'kor', 'cn', 'tw', 'hk'}
    return bool(parts) and all(p in atoms for p in parts)

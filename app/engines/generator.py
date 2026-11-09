"""Mock 数据生成引擎：字段表生成、JSON Schema 生成、单值生成。

设计要点：

* **纯 Python + 确定性**：所有随机都来自 ``random.Random(seed)`` 的**局部实例**，
  绝不触碰全局 ``random``；相同 ``seed`` 一定产出逐字节相同的结果，不同 ``seed``
  产出不同结果。``uuid`` 也由种子派生的随机字节构造（不用 ``uuid.uuid4()``，
  那个走操作系统熵源，无法复现）。
* **只读**：绝不修改传入的 ``FieldSpec`` / schema；从 schema 里取出的
  ``const`` / ``default`` / ``examples`` 一律先做迭代式深拷贝再返回。
* **有界递归**：字段嵌套与 schema 展开都有深度上限（:data:`MAX_SPEC_DEPTH` /
  :data:`MAX_SCHEMA_DEPTH`），自引用 ``$ref`` 一定会终止。
* **绝不抛异常**：非法类型名、非法枚举、畸形 schema 都退化成安全值 + 警告。
* **原生数字**：生成结果使用 ``int`` / ``float``，**不是**
  :class:`~app.core.jsonvalue.JsonNumber`（生成的是新数据，不存在"保留字面量"
  的需求）；数字一定落在 ``minimum`` / ``maximum`` 闭区间内。

``FieldSpec.pattern`` 与 JSON Schema 的 ``pattern`` 是两套机制：

* 字段表用的是**占位符模板**：``#`` 数字、``A`` 大写字母、``a`` 小写字母，
  其余字符原样输出（例如 ``"###-AA"`` -> ``"483-QP"``）。
* schema 用的是**正则**，由 :func:`_expand_regex` 支持的子集生成；子集之外
  （环视、反向引用、嵌套过深等）会给出占位串并追加一条点名该正则的警告。
"""

from __future__ import annotations

import random
import re
import string as _string
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

FIELD_TYPES: Tuple[str, ...] = (
    "string", "integer", "number", "boolean", "null", "date", "datetime", "email",
    "phone", "name", "uuid", "address", "company", "color", "ip", "word",
    "paragraph", "enum", "object", "array",
)

#: 支持的地区（其余一律按 ``en`` 处理）
LOCALES: Tuple[str, ...] = ("zh", "en")

#: 单次生成的记录条数上限，避免 UI 填错数字把内存吃光
MAX_GENERATE_COUNT = 100_000
#: FieldSpec 嵌套（children / items）的最大展开层数
MAX_SPEC_DEPTH = 32
#: JSON Schema 的最大展开层数（同时用于自引用 $ref 的终止）
MAX_SCHEMA_DEPTH = 64
#: 数组元素个数上限
MAX_ARRAY_LENGTH = 10_000
#: 字符串长度上限
MAX_STRING_LENGTH = 4096

__all__ = [
    "FIELD_TYPES",
    "FieldSpec",
    "LOCALES",
    "MAX_ARRAY_LENGTH",
    "MAX_GENERATE_COUNT",
    "MAX_SCHEMA_DEPTH",
    "MAX_SPEC_DEPTH",
    "MAX_STRING_LENGTH",
    "MockOptions",
    "field_spec_from_row",
    "generate_from_fields",
    "generate_from_schema",
    "mock_value",
    "path_string",
]


# --------------------------------------------------------------------------- #
# 路径文本（三个引擎模块共用同一份实现；因约定不新建共享文件而各自复制一份，
# 修改时必须同步 statistics.py / scanners.py / generator.py 三处）
# --------------------------------------------------------------------------- #
_PATH_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def path_string(path: Tuple) -> str:
    """把路径元组转成规范 JSONPath 文本。

    规则：对象键是标识符形式时写作 ``$.a.b``，否则用单引号括号形式
    ``$['weird key']``（内部的 ``\\`` 与 ``'`` 需转义）；数组下标写作 ``[0]``；
    根路径写作 ``$``。
    """
    parts = ["$"]
    try:
        items = tuple(path)
    except TypeError:
        return "$"
    for item in items:
        if isinstance(item, bool):  # bool 是 int 子类，先挡住
            parts.append("[%d]" % int(item))
        elif isinstance(item, int):
            parts.append(f"[{item}]")
        else:
            key = item if isinstance(item, str) else str(item)
            if _PATH_IDENT.match(key):
                parts.append("." + key)
            else:
                if "'" in key:
                    parts.append('["' + key.replace("\\", "\\\\").replace('"', '\\"') + '"]')
                else:
                    parts.append("['" + key.replace("\\", "\\\\") + "']")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# 选项与字段规格
# --------------------------------------------------------------------------- #
@dataclass
class MockOptions:
    """生成选项。``seed`` 给定则输出完全可复现。"""

    locale: str = "zh"
    seed: Optional[int] = None
    count: int = 1

    def normalized(self) -> "MockOptions":
        locale = "zh" if str(self.locale or "zh").lower().startswith("zh") else "en"
        try:
            count = int(self.count)
        except (TypeError, ValueError):
            count = 1
        return MockOptions(locale=locale, seed=self.seed, count=max(1, count))


@dataclass
class FieldSpec:
    """UI 字段表里的一行（可嵌套）。"""

    name: str = "field"
    type: str = "string"
    required: bool = True
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    length: Optional[int] = None
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    enum: Tuple = ()
    pattern: str = ""
    children: Tuple = ()
    items: Optional["FieldSpec"] = None
    array_length: int = 3

    def normalized(self) -> "FieldSpec":
        """返回一份"可直接使用"的副本：类型名合法、长度区间自洽、children 归一。"""
        kind = str(self.type or "string").strip().lower()
        if kind not in FIELD_TYPES:
            kind = "string"
        minimum = _as_float(self.minimum)
        maximum = _as_float(self.maximum)
        if minimum is not None and maximum is not None and minimum > maximum:
            minimum, maximum = maximum, minimum
        length = _clamp_int(self.length, 0, MAX_STRING_LENGTH)
        min_length = _clamp_int(self.min_length, 0, MAX_STRING_LENGTH)
        max_length = _clamp_int(self.max_length, 0, MAX_STRING_LENGTH)
        if length is not None:
            min_length = max_length = length
        else:
            if min_length is not None and max_length is not None and min_length > max_length:
                min_length, max_length = max_length, min_length
            if min_length is not None and max_length is None:
                max_length = min(min_length + 16, MAX_STRING_LENGTH)
            if max_length is not None and min_length is None:
                min_length = min(max_length, 8)
        children = tuple(
            child if isinstance(child, FieldSpec) else field_spec_from_row(child)
            for child in (self.children or ())
        )
        items = self.items
        if items is not None and not isinstance(items, FieldSpec):
            items = field_spec_from_row(items)
        try:
            array_length = int(self.array_length)
        except (TypeError, ValueError):
            array_length = 3
        return FieldSpec(
            name=str(self.name or "field"),
            type=kind,
            required=bool(self.required),
            minimum=minimum,
            maximum=maximum,
            length=length,
            min_length=min_length,
            max_length=max_length,
            enum=tuple(self.enum or ()),
            pattern=str(self.pattern or ""),
            children=children,
            items=items,
            array_length=max(0, min(array_length, MAX_ARRAY_LENGTH)),
        )


# --------------------------------------------------------------------------- #
# 语料库（离线内置，绝不联网）
# --------------------------------------------------------------------------- #
_ZH_SURNAMES = (
    "王", "李", "张", "刘", "陈", "杨", "黄", "赵", "吴", "周", "徐", "孙", "马", "朱",
    "胡", "郭", "何", "高", "林", "罗", "郑", "梁", "谢", "宋", "唐", "许", "韩", "冯",
    "邓", "曹", "彭", "曾", "肖", "田", "董", "袁", "潘", "于", "蒋", "蔡", "余", "杜",
)
_ZH_GIVEN_NAMES = (
    "伟", "芳", "娜", "敏", "静", "丽", "强", "磊", "军", "洋", "勇", "艳", "杰", "娟",
    "涛", "明", "超", "霞", "平", "刚", "桂英", "秀兰", "建国", "志强", "海燕", "晓明",
    "子轩", "雨泽", "浩然", "欣怡", "梓涵", "宇轩", "一鸣", "嘉怡", "思远", "俊杰",
    "文博", "晨曦", "若曦", "泽宇", "家豪", "明轩", "昊天", "语嫣", "诗涵", "皓轩",
)
_ZH_PINYIN = (
    "wang", "li", "zhang", "liu", "chen", "yang", "huang", "zhao", "wu", "zhou",
    "xu", "sun", "ma", "zhu", "hu", "guo", "he", "gao", "lin", "luo", "zheng",
    "liang", "xie", "song", "tang", "han", "feng", "deng", "cao", "peng",
)
_ZH_PINYIN_GIVEN = (
    "wei", "fang", "na", "min", "jing", "li", "qiang", "lei", "jun", "yang",
    "yong", "yan", "jie", "tao", "ming", "chao", "xia", "ping", "gang", "hao",
    "ran", "yi", "ze", "han", "xuan", "bo", "chen", "rui", "ning", "kai",
)
_ZH_DOMAINS = (
    "qq.com", "163.com", "126.com", "sina.com", "foxmail.com", "aliyun.com",
    "139.com", "yeah.net", "gmail.com", "outlook.com",
)
#: 中国大陆手机号段（13x/14x/15x/16x/17x/18x/19x）
_ZH_PHONE_PREFIXES = (
    "133", "135", "136", "137", "138", "139", "145", "147", "149", "150", "151",
    "152", "153", "155", "156", "157", "158", "159", "166", "170", "171", "172",
    "173", "175", "176", "177", "178", "180", "181", "182", "183", "184", "185",
    "186", "187", "188", "189", "191", "193", "195", "198", "199",
)
#: (省, 市, 区县) 三元组，保证地址组合真实可读
_ZH_REGIONS = (
    ("北京市", "北京市", ("朝阳区", "海淀区", "东城区", "西城区", "丰台区", "通州区")),
    ("上海市", "上海市", ("浦东新区", "黄浦区", "徐汇区", "长宁区", "静安区", "闵行区")),
    ("天津市", "天津市", ("和平区", "南开区", "河西区", "河东区", "滨海新区")),
    ("重庆市", "重庆市", ("渝中区", "江北区", "沙坪坝区", "九龙坡区", "渝北区")),
    ("广东省", "广州市", ("天河区", "越秀区", "海珠区", "白云区", "番禺区")),
    ("广东省", "深圳市", ("福田区", "南山区", "罗湖区", "宝安区", "龙岗区")),
    ("江苏省", "南京市", ("鼓楼区", "玄武区", "秦淮区", "建邺区", "江宁区")),
    ("江苏省", "苏州市", ("姑苏区", "吴中区", "相城区", "工业园区")),
    ("浙江省", "杭州市", ("西湖区", "上城区", "拱墅区", "滨江区", "余杭区")),
    ("浙江省", "宁波市", ("海曙区", "江北区", "鄞州区", "镇海区")),
    ("四川省", "成都市", ("锦江区", "武侯区", "青羊区", "成华区", "高新区")),
    ("湖北省", "武汉市", ("武昌区", "江汉区", "洪山区", "汉阳区", "江岸区")),
    ("陕西省", "西安市", ("雁塔区", "碑林区", "莲湖区", "新城区", "未央区")),
    ("山东省", "青岛市", ("市南区", "市北区", "崂山区", "黄岛区")),
    ("湖南省", "长沙市", ("芙蓉区", "天心区", "岳麓区", "开福区", "雨花区")),
    ("福建省", "厦门市", ("思明区", "湖里区", "集美区", "海沧区")),
    ("河南省", "郑州市", ("金水区", "中原区", "二七区", "管城回族区")),
    ("辽宁省", "大连市", ("中山区", "西岗区", "沙河口区", "甘井子区")),
    ("安徽省", "合肥市", ("蜀山区", "庐阳区", "包河区", "瑶海区")),
    ("云南省", "昆明市", ("盘龙区", "五华区", "官渡区", "西山区")),
)
_ZH_STREETS = (
    "中山路", "人民路", "解放路", "建设路", "文化路", "学院路", "科技大道", "滨江大道",
    "花园路", "和平路", "新华街", "复兴路", "朝阳路", "长江路", "黄河路", "东风路",
    "友谊路", "振兴街", "长安街", "南京路",
)
_ZH_COMPANY_BRANDS = (
    "华信", "恒通", "远景", "中科", "博纳", "天工", "嘉合", "瑞丰", "启明", "宏泰",
    "新程", "联创", "智谷", "金穗", "海纳", "星辰", "卓越", "汇通", "同方", "云图",
)
_ZH_COMPANY_INDUSTRIES = (
    "科技", "信息科技", "网络科技", "软件", "数据服务", "智能装备", "生物医药",
    "新材料", "电子商务", "文化传媒", "供应链", "云计算",
)
_ZH_COMPANY_SUFFIXES = ("有限公司", "股份有限公司", "有限责任公司", "集团有限公司")
_ZH_WORDS = (
    "数据", "接口", "配置", "用户", "系统", "服务", "模块", "参数", "缓存", "日志",
    "报表", "权限", "流程", "任务", "队列", "索引", "事务", "协议", "字段", "节点",
    "集群", "网关", "算法", "模型", "脚本", "架构", "监控", "部署", "回滚", "灰度",
)
_ZH_CONNECTORS = ("的", "与", "和", "以及", "对于", "在", "通过", "基于")
_EN_FIRST_NAMES = (
    "James", "Mary", "John", "Patricia", "Robert", "Jennifer", "Michael", "Linda",
    "William", "Elizabeth", "David", "Barbara", "Richard", "Susan", "Joseph",
    "Jessica", "Thomas", "Sarah", "Charles", "Karen", "Daniel", "Nancy",
)
_EN_LAST_NAMES = (
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
    "Rodriguez", "Martinez", "Hernandez", "Lopez", "Wilson", "Anderson", "Thomas",
    "Taylor", "Moore", "Jackson", "Martin", "Lee", "Clark", "Walker",
)
_EN_DOMAINS = (
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "protonmail.com",
    "example.com", "fastmail.com", "corp-mail.com",
)
_EN_STREETS = (
    "Main St", "Oak Ave", "Maple Dr", "Cedar Ln", "Pine Rd", "Elm St",
    "Washington Blvd", "Park Ave", "Lake View Rd", "Highland Ave", "Sunset Blvd",
)
_EN_CITIES = (
    ("Austin", "TX", "787"), ("Seattle", "WA", "981"), ("Denver", "CO", "802"),
    ("Boston", "MA", "021"), ("Chicago", "IL", "606"), ("Miami", "FL", "331"),
    ("Atlanta", "GA", "303"), ("Phoenix", "AZ", "850"), ("Dallas", "TX", "752"),
    ("Portland", "OR", "972"), ("San Diego", "CA", "921"), ("Detroit", "MI", "482"),
)
_EN_COMPANY_BRANDS = (
    "Acme", "Globex", "Initech", "Umbrella", "Hooli", "Vandelay", "Soylent",
    "Cyberdyne", "Wonka", "Tyrell", "Stark", "Wayne", "Pied Piper", "Gekko",
    "Northwind", "Contoso", "Fabrikam", "Lumen",
)
_EN_COMPANY_SUFFIXES = (
    "Industries", "Systems", "Labs", "Solutions", "Group", "Technologies",
    "Partners", "Holdings", "Dynamics", "Works",
)
_EN_WORDS = (
    "lorem", "ipsum", "dolor", "sit", "amet", "consectetur", "adipiscing", "elit",
    "sed", "eiusmod", "tempor", "incididunt", "ut", "labore", "et", "dolore",
    "magna", "aliqua", "enim", "ad", "minim", "veniam", "quis", "nostrud",
    "exercitation", "ullamco", "laboris", "nisi", "aliquip", "ex", "ea", "commodo",
    "consequat", "duis", "aute", "irure", "reprehenderit", "voluptate", "velit",
)
_ASCII_LETTERS = _string.ascii_letters
_DIGITS = _string.digits
_TOKEN_CHARS = _string.ascii_lowercase + _string.digits

_DATE_MIN = date(1970, 1, 1).toordinal()
_DATE_MAX = date(2035, 12, 31).toordinal()


# --------------------------------------------------------------------------- #
# 生成上下文
# --------------------------------------------------------------------------- #
class _Ctx:
    """一次生成的共享状态：随机源、地区、警告列表。"""

    __slots__ = ("rng", "locale", "warnings", "options", "_seen")

    def __init__(self, options: Optional[MockOptions]) -> None:
        self.options = options.normalized() if isinstance(options, MockOptions) else MockOptions()
        self.locale = self.options.locale
        self.warnings: List[str] = []
        self._seen: set = set()
        seed = self.options.seed
        try:
            self.rng = random.Random(seed) if seed is not None else random.Random()
        except (TypeError, ValueError):  # 种子类型不合法时退化成不可复现
            self.warn(f"seed {seed!r} is not usable; output is not reproducible")
            self.rng = random.Random()

    def warn(self, text: str) -> None:
        """记录一条英文警告；同文本只记一次（同一个子 schema 会被反复访问）。"""
        if text in self._seen:
            return
        self._seen.add(text)
        self.warnings.append(text)

    # -- 随机原语（全部走局部 Random 实例） --
    def int(self, low: int, high: int) -> int:
        if high < low:
            low, high = high, low
        return self.rng.randint(low, high)

    def choice(self, seq: Sequence) -> Any:
        return self.rng.choice(list(seq))

    def pick_many(self, seq: Sequence, count: int) -> List[Any]:
        pool = list(seq)
        count = max(0, min(count, len(pool)))
        return self.rng.sample(pool, count)

    def random(self) -> float:
        return self.rng.random()

    def bytes(self, count: int) -> bytes:
        return self.rng.randbytes(max(0, count))


def _as_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _as_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return int(result)


def _clamp_int(value: Any, low: int, high: int) -> Optional[int]:
    number = _as_int(value)
    if number is None:
        return None
    return max(low, min(high, number))


# --------------------------------------------------------------------------- #
# 单个 mock 值
# --------------------------------------------------------------------------- #
def mock_value(kind: str, options: Optional[MockOptions] = None) -> Any:
    """按类型名生成一个 mock 值。未知类型退化成字符串，绝不抛异常。

    ``object`` / ``array`` 需要子字段定义，裸调用返回空容器；要生成有内容的
    结构请用 :func:`generate_from_fields` 配合 :class:`FieldSpec`。
    """
    ctx = _Ctx(options)
    return _mock_value(kind, ctx)


def _mock_value(kind: str, ctx: _Ctx) -> Any:
    name = str(kind or "string").strip().lower()
    rng = ctx.rng
    if name == "integer":
        return ctx.int(0, 1000)
    if name == "number":
        return round(ctx.int(0, 100000) / 100.0, 2)
    if name == "boolean":
        return bool(rng.randint(0, 1))
    if name == "null":
        return None
    if name == "date":
        return date.fromordinal(ctx.int(_DATE_MIN, _DATE_MAX)).isoformat()
    if name == "datetime":
        day = date.fromordinal(ctx.int(_DATE_MIN, _DATE_MAX)).isoformat()
        return f"{day}T{ctx.int(0, 23):02d}:{ctx.int(0, 59):02d}:{ctx.int(0, 59):02d}"
    if name == "email":
        return _email(ctx)
    if name == "phone":
        return _phone(ctx)
    if name == "name":
        return _person_name(ctx)
    if name == "uuid":
        # 由种子派生的随机字节构造，保证可复现（uuid.uuid4() 走系统熵源不可复现）
        return str(uuid.UUID(bytes=ctx.bytes(16), version=4))
    if name == "address":
        return _address(ctx)
    if name == "company":
        return _company(ctx)
    if name == "color":
        return "#%06x" % ctx.int(0, 0xFFFFFF)
    if name == "ip":
        return ".".join(str(ctx.int(0, 255)) for _ in range(4))
    if name == "word":
        return _word(ctx)
    if name == "paragraph":
        return _paragraph(ctx)
    if name == "string":
        return _generic_string(ctx)
    if name == "enum":
        return _word(ctx)
    if name == "object":
        return {}
    if name == "array":
        return []
    return _generic_string(ctx)


def _token(ctx: _Ctx, low: int, high: int) -> str:
    """随机 ASCII 令牌（小写字母 + 数字），长度闭区间 [low, high]。"""
    low = max(0, min(int(low), MAX_STRING_LENGTH))
    high = max(low, min(int(high), MAX_STRING_LENGTH))
    length = ctx.int(low, high)
    return "".join(ctx.choice(_TOKEN_CHARS) for _ in range(length))


def _generic_string(ctx: _Ctx, low: int = 8, high: int = 16) -> str:
    """通用字符串：中文地区给中文词串，英文地区给 ASCII 令牌；长度在闭区间内。"""
    low = max(0, min(int(low), MAX_STRING_LENGTH))
    high = max(low, min(int(high), MAX_STRING_LENGTH))
    length = ctx.int(low, high)
    if ctx.locale == "zh":
        return _locale_text(ctx, length)
    return _token(ctx, length, length)


def _person_name(ctx: _Ctx) -> str:
    if ctx.locale == "zh":
        return ctx.choice(_ZH_SURNAMES) + ctx.choice(_ZH_GIVEN_NAMES)
    return f"{ctx.choice(_EN_FIRST_NAMES)} {ctx.choice(_EN_LAST_NAMES)}"


def _phone(ctx: _Ctx) -> str:
    if ctx.locale == "zh":
        return ctx.choice(_ZH_PHONE_PREFIXES) + "".join(
            ctx.choice(_DIGITS) for _ in range(8))
    # 北美编号计划：区号与交换码首位不能是 0/1
    area = ctx.int(200, 999)
    exchange = ctx.int(200, 999)
    return f"({area}) {exchange}-{ctx.int(0, 9999):04d}"


def _email(ctx: _Ctx) -> str:
    if ctx.locale == "zh":
        local = ctx.choice(_ZH_PINYIN) + ctx.choice(_ZH_PINYIN_GIVEN)
    else:
        first = ctx.choice(_EN_FIRST_NAMES).lower()
        last = ctx.choice(_EN_LAST_NAMES).lower()
        local = ctx.rng.choice([first + last, first + "." + last, last + first])
    if ctx.int(0, 1):
        local += str(ctx.int(1, 9999))
    domain = ctx.choice(_ZH_DOMAINS if ctx.locale == "zh" else _EN_DOMAINS)
    return f"{local}@{domain}"


def _address(ctx: _Ctx) -> str:
    if ctx.locale == "zh":
        province, city, districts = ctx.choice(_ZH_REGIONS)
        parts = [province]
        if city != province:
            parts.append(city)
        parts.append(ctx.choice(districts))
        parts.append(ctx.choice(_ZH_STREETS))
        parts.append(f"{ctx.int(1, 999)}号")
        if ctx.int(0, 1):
            parts.append(f"{ctx.int(1, 30)}栋{ctx.int(101, 2508)}室")
        return "".join(parts)
    city, state, zip_prefix = ctx.choice(_EN_CITIES)
    return (f"{ctx.int(100, 9999)} {ctx.choice(_EN_STREETS)}, {city}, "
            f"{state} {zip_prefix}{ctx.int(10, 99)}")


def _company(ctx: _Ctx) -> str:
    if ctx.locale == "zh":
        _province, city, _districts = ctx.choice(_ZH_REGIONS)
        place = city.replace("市", "")
        return (place + ctx.choice(_ZH_COMPANY_BRANDS)
                + ctx.choice(_ZH_COMPANY_INDUSTRIES)
                + ctx.choice(_ZH_COMPANY_SUFFIXES))
    brand = ctx.choice(_EN_COMPANY_BRANDS)
    return f"{brand} {ctx.choice(_EN_COMPANY_SUFFIXES)}"


def _word(ctx: _Ctx) -> str:
    return ctx.choice(_ZH_WORDS if ctx.locale == "zh" else _EN_WORDS)


def _sentence(ctx: _Ctx) -> str:
    if ctx.locale == "zh":
        parts = [_word(ctx)]
        for _ in range(ctx.int(1, 3)):
            parts.append(ctx.choice(_ZH_CONNECTORS) + _word(ctx))
        return "".join(parts) + "，" + _word(ctx) + "需要进一步确认。"
    words = [_word(ctx)]
    for _ in range(ctx.int(4, 12)):
        words.append(_word(ctx))
    return " ".join(words).capitalize() + "."


def _paragraph(ctx: _Ctx) -> str:
    joiner = "" if ctx.locale == "zh" else " "
    return joiner.join(_sentence(ctx) for _ in range(ctx.int(2, 4)))


def _locale_text(ctx: _Ctx, length: int) -> str:
    """按目标长度拼出地区化文本（先拼词再精确截断，保证长度正好）。"""
    length = max(0, min(int(length), MAX_STRING_LENGTH))
    if length == 0:
        return ""
    joiner = "" if ctx.locale == "zh" else " "
    chunks: List[str] = []
    total = 0
    while total < length:
        word = _word(ctx)
        chunks.append(word)
        total += len(word) + (len(joiner) if len(chunks) > 1 else 0)
    text = joiner.join(chunks)
    if len(text) < length:  # 极端情况（length=1 且词更长时不会发生）兜底填充
        filler = "字" if ctx.locale == "zh" else "a"
        text += filler * (length - len(text))
    return text[:length]


# --------------------------------------------------------------------------- #
# FieldSpec 占位符模板
# --------------------------------------------------------------------------- #
def _expand_template(template: str, ctx: _Ctx) -> str:
    """``#`` 数字 / ``A`` 大写字母 / ``a`` 小写字母，其余字符原样输出。"""
    out: List[str] = []
    for ch in template[:MAX_STRING_LENGTH]:
        if ch == "#":
            out.append(ctx.choice(_DIGITS))
        elif ch == "A":
            out.append(ctx.choice(_string.ascii_uppercase))
        elif ch == "a":
            out.append(ctx.choice(_string.ascii_lowercase))
        else:
            out.append(ch)
    return "".join(out)


def _spec_value(spec: FieldSpec, ctx: _Ctx, depth: int) -> Any:
    """按字段规格生成一个值；``depth`` 用于阻断自引用的 FieldSpec。"""
    if spec.enum:
        return ctx.choice(spec.enum)
    kind = spec.type
    if kind == "object":
        if depth >= MAX_SPEC_DEPTH:
            ctx.warnings.append(
                f"field '{spec.name}': object nesting deeper than {MAX_SPEC_DEPTH}"
                " levels; emitted an empty object")
            return {}
        record: Dict[str, Any] = {}
        for index, child in enumerate(spec.children):
            key = child.name or f"field{index + 1}"
            record[key] = _spec_value(child, ctx, depth + 1)
        return record
    if kind == "array":
        if depth >= MAX_SPEC_DEPTH:
            ctx.warnings.append(
                f"field '{spec.name}': array nesting deeper than {MAX_SPEC_DEPTH}"
                " levels; emitted an empty array")
            return []
        item_spec = spec.items
        if item_spec is None:
            item_spec = FieldSpec(name=f"{spec.name}Item", type="string")
        return [_spec_value(item_spec, ctx, depth + 1) for _ in range(spec.array_length)]
    if kind == "string" and spec.pattern:
        return _expand_template(spec.pattern, ctx)
    if kind in ("integer", "number"):
        return _bounded_number(spec, ctx, kind == "integer")
    if kind == "string":
        low = spec.min_length if spec.min_length is not None else 8
        high = spec.max_length if spec.max_length is not None else 16
        if spec.length is not None:
            low = high = spec.length
        return _generic_string(ctx, low, high)
    return _mock_value(kind, ctx)


def _bounded_number(spec: FieldSpec, ctx: _Ctx, integer: bool) -> Any:
    """在 ``minimum`` / ``maximum`` 闭区间内生成数字（含区间端点）。"""
    low = spec.minimum
    high = spec.maximum
    if integer:
        low_int = int(low) if low is not None else 0
        high_int = int(high) if high is not None else 1000
        if low is not None and low > low_int:
            low_int += 1  # 向上取整，保证 >= minimum
        if high is not None and high < high_int:
            high_int -= 1
        if low_int > high_int:
            low_int = high_int
        return ctx.int(low_int, high_int)
    low_value = float(low) if low is not None else 0.0
    high_value = float(high) if high is not None else 1000.0
    if low_value > high_value:
        low_value, high_value = high_value, low_value
    value = round(low_value + (high_value - low_value) * ctx.random(), 2)
    # 两位小数四舍五入可能把值推出极窄区间，夹回来保证闭区间承诺
    return min(max(value, low_value), high_value)


# --------------------------------------------------------------------------- #
# 字段表入口
# --------------------------------------------------------------------------- #
def _coerce_spec(item: Any) -> FieldSpec:
    if isinstance(item, FieldSpec):
        return item.normalized()
    return field_spec_from_row(item)


def generate_from_fields(fields: Any, *, root: str = "object", count: int = 1,
                         options: Optional[MockOptions] = None) -> Any:
    """按字段表生成 mock 数据。

    * ``root="object"``：返回一个对象；``count > 1`` 时返回 ``count`` 个对象组成的
      数组（单个对象放不下多条记录）。
    * ``root="array"``：始终返回长度为 ``max(1, count)`` 的数组，元素是按 ``fields``
      构造的对象。
    * ``count`` 未显式给出（等于默认值 1）时回退到 ``options.count``；结果会被夹到
      ``[1, MAX_GENERATE_COUNT]``。

    字段名重复时后者覆盖前者（与 JSON 语义一致）；``fields`` 里的元素可以是
    :class:`FieldSpec`，也可以是 UI 表格给出的 ``dict``。
    """
    ctx = _Ctx(options)
    specs: List[FieldSpec] = []
    try:
        for item in fields or ():
            specs.append(_coerce_spec(item))
    except TypeError:
        specs = [_coerce_spec(fields)]
    try:
        requested = int(count)
    except (TypeError, ValueError):
        requested = 1
    if requested <= 1:
        requested = ctx.options.count
    total = max(1, min(requested, MAX_GENERATE_COUNT))
    is_array = str(root or "object").strip().lower() == "array"
    if is_array:
        return [_record(specs, ctx) for _ in range(total)]
    if total == 1:
        return _record(specs, ctx)
    return [_record(specs, ctx) for _ in range(total)]


def _record(specs: Sequence[FieldSpec], ctx: _Ctx) -> Dict[str, Any]:
    record: Dict[str, Any] = {}
    for index, spec in enumerate(specs):
        key = spec.name or f"field{index + 1}"
        record[key] = _spec_value(spec, ctx, 0)
    return record


# --------------------------------------------------------------------------- #
# UI 表格行 -> FieldSpec
# --------------------------------------------------------------------------- #
_ROW_ALIASES: Dict[str, Tuple[str, ...]] = {
    "name": ("name", "field", "fieldname", "key", "label", "title"),
    "type": ("type", "kind", "fieldtype", "datatype", "valuetype"),
    "required": ("required", "mandatory", "notnull", "isrequired"),
    "minimum": ("minimum", "min", "minvalue", "minval"),
    "maximum": ("maximum", "max", "maxvalue", "maxval"),
    "length": ("length", "len", "size", "exactlength"),
    "min_length": ("minlength", "minlen", "minchars", "minsize"),
    "max_length": ("maxlength", "maxlen", "maxchars", "maxsize"),
    "enum": ("enum", "values", "options", "choices", "enumeration"),
    "pattern": ("pattern", "template", "mask", "format"),
    "children": ("children", "properties", "fields", "subfields", "objectfields"),
    "items": ("items", "item", "element", "elements", "arrayitems"),
    "array_length": ("arraylength", "itemscount", "itemcount", "count", "length2"),
}

_TRUTHY = {"1", "y", "yes", "true", "t", "on", "required", "是", "必填"}


def _normalize_row_keys(row: Mapping) -> Dict[str, Any]:
    normalized: Dict[str, Any] = {}
    for key, value in row.items():
        token = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(key).lower())
        if token and token not in normalized:
            normalized[token] = value
    return normalized


def _row_get(normalized: Mapping, field_name: str) -> Any:
    for alias in _ROW_ALIASES.get(field_name, (field_name,)):
        if alias in normalized:
            value = normalized[alias]
            if isinstance(value, str) and not value.strip():
                continue  # 空单元格视为未填写
            if value is None:
                continue
            return value
    return None


def _row_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if not text:
        return default
    if text in ("0", "n", "no", "false", "f", "off", "optional", "否", "可选"):
        return False
    return text in _TRUTHY or default


def _row_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if isinstance(value, FieldSpec):
        return [value]
    if isinstance(value, Mapping):
        return [value]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return [part.strip().strip("'\"") for part in re.split(r"[,;|]", text) if part.strip()]


def field_spec_from_row(row: Any) -> FieldSpec:
    """把 UI 表格的一行转成 :class:`FieldSpec`，对缺失/空白/非法单元格全部容错。

    接受的键名大小写与分隔符不敏感（``min_length`` / ``minLength`` / ``min length``
    等价），并支持常见别名（``field``/``key`` 当作 ``name``，``values``/``options``
    当作 ``enum`` 等）。``enum`` 可以写成列表，也可以写成 ``"a,b,c"``。
    """
    try:
        if isinstance(row, FieldSpec):
            return row.normalized()
        if not isinstance(row, Mapping):
            if row is None:
                return FieldSpec().normalized()
            return FieldSpec(name=str(row).strip() or "field").normalized()
        normalized = _normalize_row_keys(row)
        name = _row_get(normalized, "name")
        kind = _row_get(normalized, "type")
        kind_text = str(kind).strip().lower() if kind is not None else ""
        if kind_text and kind_text not in FIELD_TYPES:
            kind_text = "string"  # 未知类型名一律按字符串处理，绝不抛错
        children_raw = _row_list(_row_get(normalized, "children"))
        children = tuple(
            child if isinstance(child, FieldSpec) else field_spec_from_row(child)
            for child in children_raw
        )
        items_raw = _row_get(normalized, "items")
        items_list = _row_list(items_raw)
        items: Optional[FieldSpec] = None
        if items_list:
            first = items_list[0]
            items = first if isinstance(first, FieldSpec) else field_spec_from_row(first)
        enum_values = tuple(
            item for item in _row_list(_row_get(normalized, "enum"))
        )
        return FieldSpec(
            name=str(name).strip() if name is not None else "field",
            type=kind_text or "string",
            required=_row_bool(_row_get(normalized, "required"), True),
            minimum=_as_float(_row_get(normalized, "minimum")),
            maximum=_as_float(_row_get(normalized, "maximum")),
            length=_clamp_int(_row_get(normalized, "length"), 0, MAX_STRING_LENGTH),
            min_length=_clamp_int(_row_get(normalized, "min_length"), 0, MAX_STRING_LENGTH),
            max_length=_clamp_int(_row_get(normalized, "max_length"), 0, MAX_STRING_LENGTH),
            enum=enum_values,
            pattern=str(_row_get(normalized, "pattern") or ""),
            children=children,
            items=items,
            array_length=int(_as_int(_row_get(normalized, "array_length"))
                             if _as_int(_row_get(normalized, "array_length")) is not None
                             else 3),
        ).normalized()
    except Exception:  # pragma: no cover - 防御：表格数据再离谱也不能崩
        return FieldSpec().normalized()


# --------------------------------------------------------------------------- #
# JSON Schema -> mock 数据
# --------------------------------------------------------------------------- #
_SUPPORTED_TYPES: Tuple[str, ...] = (
    "object", "array", "string", "number", "integer", "boolean", "null",
)
_SCALAR_TYPES: Tuple[str, ...] = ("string", "number", "integer", "boolean", "null")

#: format -> mock 类型（format 在 JSON Schema 里只是注解，映射后仍然是合法字符串）
_FORMAT_KINDS: Dict[str, str] = {
    "date-time": "datetime",
    "date": "date",
    "email": "email",
    "idn-email": "email",
    "uuid": "uuid",
    "ipv4": "ip",
    "hostname": "word",
    "color": "color",
    "phone": "phone",
    "address": "address",
}

#: 无法在生成阶段保证语义的关键字：只做提示，不假装支持
_UNSUPPORTED_KEYWORDS: Tuple[str, ...] = (
    "not", "if", "then", "else", "dependentSchemas", "dependentRequired",
    "patternProperties", "propertyNames", "contains", "unevaluatedProperties",
    "unevaluatedItems", "contentSchema", "prefixItemsValidation",
)

_MAX_REF_HOPS = 32
_MAX_ALL_OF_NESTING = 8
_MAX_REGEX_GROUP_DEPTH = 8
_MAX_REGEX_QUANTIFIER = 64
_MAX_PATTERN_BUDGET = 512


class _SchemaCtx(_Ctx):
    """schema 生成上下文：额外携带根 schema（供 ``$ref`` 解析）与深度预算。"""

    __slots__ = ("root", "max_depth", "array_items")

    def __init__(self, options: Optional[MockOptions], root: Any, max_depth: int,
                 array_items: int) -> None:
        super().__init__(options)
        self.root = root
        self.max_depth = max_depth
        self.array_items = array_items


def _clone_value(value: Any) -> Any:
    """迭代式深拷贝 JSON 值，避免把 schema 内部对象直接交给调用方（调用方可能改它）。"""
    if isinstance(value, Mapping):
        root: Any = {}
    elif isinstance(value, (list, tuple)):
        root = []
    else:
        return value
    stack: List[Tuple[Any, Any]] = [(value, root)]
    while stack:
        source, target = stack.pop()
        if isinstance(source, Mapping):
            for key, item in source.items():
                if isinstance(item, Mapping):
                    child: Any = {}
                    target[key] = child
                    stack.append((item, child))
                elif isinstance(item, (list, tuple)):
                    child = []
                    target[key] = child
                    stack.append((item, child))
                else:
                    target[key] = item
        else:
            for item in source:
                if isinstance(item, Mapping):
                    child = {}
                    target.append(child)
                    stack.append((item, child))
                elif isinstance(item, (list, tuple)):
                    child = []
                    target.append(child)
                    stack.append((item, child))
                else:
                    target.append(item)
    return root


def _json_kind(value: Any) -> str:
    """JSON 类型名，但把整数与浮点分开（``integer`` / ``number``）。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    return "string"


def _loose_equal(left: Any, right: Any) -> bool:
    """JSON 语义的相等（``True`` 与 ``1`` 不算相等）。"""
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    try:
        return bool(left == right)
    except Exception:  # pragma: no cover - 防御
        return False


def _canonical(value: Any) -> Any:
    """把值转成可哈希的规范形式，供 ``uniqueItems`` 去重使用。

    只作用于**本模块生成的值**（深度受 ``max_depth`` 限制），因此递归是安全的。
    """
    if isinstance(value, Mapping):
        return ("o", tuple(sorted((str(k), _canonical(v)) for k, v in value.items())))
    if isinstance(value, (list, tuple)):
        return ("a", tuple(_canonical(item) for item in value))
    if isinstance(value, bool):
        return ("b", value)
    if value is None:
        return ("z",)
    if isinstance(value, (int, float)):
        return ("n", float(value))
    return ("s", str(value))


def _resolve_pointer(root: Any, ref: str) -> Any:
    """解析本地 ``#/a/b/0`` 形式的 JSON Pointer；失败返回 ``None``。"""
    pointer = ref[1:]
    if pointer.startswith("/"):
        pointer = pointer[1:]
    node = root
    if pointer == "":
        return root
    for raw in pointer.split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping):
            if token not in node:
                return None
            node = node[token]
        elif isinstance(node, (list, tuple)):
            try:
                index = int(token)
            except ValueError:
                return None
            if not 0 <= index < len(node):
                return None
            node = node[index]
        else:
            return None
    return node


def _resolve_ref(schema: Mapping, ctx: _SchemaCtx, path: Tuple) -> Any:
    """跟随 ``$ref``（迭代，最多 :data:`_MAX_REF_HOPS` 跳），返回可继续生成的 schema。

    * 远程 ``$ref``（不以 ``#`` 开头）离线无法取回 -> 告警并退回兄弟关键字。
    * 本地指针解析失败或成环 -> 告警并退回兄弟关键字。
    """
    current: Any = schema
    seen = set()
    for _ in range(_MAX_REF_HOPS):
        if not isinstance(current, Mapping):
            return current
        ref = current.get("$ref")
        if not isinstance(ref, str) or not ref:
            return current
        siblings = {k: v for k, v in current.items() if k != "$ref"}
        if ref in seen:
            ctx.warn(f"{path_string(path)}: $ref '{ref}' forms a cycle; ignored it")
            return siblings
        seen.add(ref)
        if not ref.startswith("#"):
            ctx.warn(f"{path_string(path)}: remote $ref '{ref}' cannot be resolved"
                     " offline; generated a value from the sibling keywords instead")
            return siblings
        target = _resolve_pointer(ctx.root, ref)
        if target is None:
            ctx.warn(f"{path_string(path)}: unresolved local $ref '{ref}'; ignored it")
            return siblings
        current = target
    ctx.warn(f"{path_string(path)}: $ref chain is longer than {_MAX_REF_HOPS} hops;"
             " stopped following it")
    return current if isinstance(current, Mapping) else {}


def _merge_into(target: Dict[str, Any], source: Mapping, ctx: _SchemaCtx,
                path: Tuple) -> None:
    """把一个子 schema 浅合并进 ``target``；已存在的关键字保留先到的那份。"""
    for key, value in source.items():
        if key in ("$schema", "$id", "title", "description", "examples", "$comment"):
            target.setdefault(key, value)
            continue
        if key == "properties" and isinstance(value, Mapping):
            existing = target.get("properties")
            if not isinstance(existing, Mapping):
                target["properties"] = dict(value)
                continue
            merged = dict(existing)
            for prop, sub in value.items():
                if prop in merged and merged[prop] != sub:
                    ctx.warn(f"{path_string(path)}: property '{prop}' is constrained by"
                             " several allOf branches; kept the first definition")
                merged.setdefault(prop, sub)
            target["properties"] = merged
            continue
        if key == "required" and isinstance(value, (list, tuple)):
            existing = target.get("required")
            names = list(existing) if isinstance(existing, (list, tuple)) else []
            for name in value:
                if name not in names:
                    names.append(name)
            target["required"] = names
            continue
        if key in ("definitions", "$defs") and isinstance(value, Mapping):
            existing = target.get(key)
            merged = dict(existing) if isinstance(existing, Mapping) else {}
            for name, sub in value.items():
                merged.setdefault(name, sub)
            target[key] = merged
            continue
        if key in target:
            if target[key] != value:
                ctx.warn(f"{path_string(path)}: allOf branches disagree on '{key}';"
                         f" kept {target[key]!r}")
            continue
        target[key] = value


def _merge_all_of(schema: Mapping, ctx: _SchemaCtx, depth: int,
                  path: Tuple) -> Dict[str, Any]:
    """浅合并 ``allOf``（连同父 schema 自身的关键字）。"""
    merged: Dict[str, Any] = {k: v for k, v in schema.items() if k != "allOf"}
    members = schema.get("allOf")
    if not isinstance(members, (list, tuple)):
        ctx.warn(f"{path_string(path)}: 'allOf' is not an array; ignored it")
        return merged
    if depth > _MAX_ALL_OF_NESTING:
        ctx.warn(f"{path_string(path)}: nested allOf deeper than {_MAX_ALL_OF_NESTING};"
                 " stopped merging")
        return merged
    for index, member in enumerate(members):
        sub_path = path + ("allOf", index)
        resolved = member
        if isinstance(resolved, Mapping) and isinstance(resolved.get("$ref"), str):
            resolved = _resolve_ref(resolved, ctx, sub_path)
        if isinstance(resolved, Mapping) and resolved.get("allOf"):
            resolved = _merge_all_of(resolved, ctx, depth + 1, sub_path)
        if not isinstance(resolved, Mapping):
            continue
        _merge_into(merged, resolved, ctx, sub_path)
    ctx.warn(f"{path_string(path)}: allOf was merged shallowly; overlapping numeric or"
             " string constraints from different branches may not all be enforced")
    return merged


def _supported_types(schema: Mapping, ctx: _SchemaCtx, path: Tuple) -> List[str]:
    raw = schema.get("type")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    supported: List[str] = []
    unknown: List[str] = []
    for item in raw:
        if isinstance(item, str) and item in _SUPPORTED_TYPES:
            if item not in supported:
                supported.append(item)
        else:
            unknown.append(str(item))
    if unknown:
        ctx.warn(f"{path_string(path)}: unsupported type(s) {', '.join(unknown)}; ignored")
    return supported


def _infer_types(schema: Mapping) -> List[str]:
    """没有 ``type`` 时按出现的关键字猜一个类型，保证结果仍然是合法实例。"""
    if any(key in schema for key in ("properties", "required", "additionalProperties",
                                     "minProperties", "maxProperties")):
        return ["object"]
    if any(key in schema for key in ("items", "prefixItems", "minItems", "maxItems",
                                     "uniqueItems")):
        return ["array"]
    if any(key in schema for key in ("minLength", "maxLength", "pattern", "format",
                                     "contentEncoding")):
        return ["string"]
    if any(key in schema for key in ("minimum", "maximum", "exclusiveMinimum",
                                     "exclusiveMaximum", "multipleOf")):
        return ["number"]
    return ["object"]


def _fits(value: Any, schema: Mapping) -> bool:
    """粗判候选值（``const`` / ``default`` / ``examples[0]``）是否满足 schema 的标量约束。"""
    if not isinstance(schema, Mapping):
        return True
    declared = schema.get("type")
    if isinstance(declared, str):
        declared = [declared]
    if isinstance(declared, (list, tuple)) and declared:
        actual = _json_kind(value)
        ok = False
        for item in declared:
            if item == actual:
                ok = True
            elif item == "number" and actual == "integer":
                ok = True
            elif (item == "integer" and actual == "number"
                  and isinstance(value, float) and value.is_integer()):
                ok = True
        if not ok:
            return False
    enum = schema.get("enum")
    if isinstance(enum, (list, tuple)) and enum:
        if not any(_loose_equal(value, item) for item in enum):
            return False
    if "const" in schema and not _loose_equal(value, schema["const"]):
        return False
    if isinstance(value, bool) or value is None:
        return True
    if isinstance(value, (int, float)):
        low = _as_float(schema.get("minimum"))
        high = _as_float(schema.get("maximum"))
        if low is not None and value < low:
            return False
        if high is not None and value > high:
            return False
        exclusive_low = schema.get("exclusiveMinimum")
        if isinstance(exclusive_low, (int, float)) and not isinstance(exclusive_low, bool):
            if value <= float(exclusive_low):
                return False
        exclusive_high = schema.get("exclusiveMaximum")
        if isinstance(exclusive_high, (int, float)) and not isinstance(exclusive_high, bool):
            if value >= float(exclusive_high):
                return False
        return True
    if isinstance(value, str):
        min_length = _as_int(schema.get("minLength"))
        max_length = _as_int(schema.get("maxLength"))
        if min_length is not None and len(value) < min_length:
            return False
        if max_length is not None and len(value) > max_length:
            return False
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and pattern:
            try:
                if re.search(pattern, value) is None:
                    return False
            except re.error:
                return True  # 正则本身不合法，交给生成阶段告警
    return True


def _ceil_int(value: float) -> int:
    truncated = int(value)
    return truncated if truncated >= value else truncated + 1


def _floor_int(value: float) -> int:
    truncated = int(value)
    return truncated if truncated <= value else truncated - 1


def _number_bounds(schema: Mapping, ctx: _SchemaCtx, path: Tuple):
    """整理出 (low, high, low_exclusive, high_exclusive, multiple_of)。"""
    low = _as_float(schema.get("minimum"))
    high = _as_float(schema.get("maximum"))
    low_exclusive = False
    high_exclusive = False
    exclusive_low = schema.get("exclusiveMinimum")
    if isinstance(exclusive_low, bool):  # draft-04：布尔修饰 minimum
        low_exclusive = exclusive_low and low is not None
    else:
        value = _as_float(exclusive_low)
        if value is not None:
            low = value if low is None else max(low, value)
            low_exclusive = True
    exclusive_high = schema.get("exclusiveMaximum")
    if isinstance(exclusive_high, bool):
        high_exclusive = exclusive_high and high is not None
    else:
        value = _as_float(exclusive_high)
        if value is not None:
            high = value if high is None else min(high, value)
            high_exclusive = True
    if low is None and high is None:
        low, high = 0.0, 1000.0
    elif low is None:
        low = high - 1000.0
    elif high is None:
        high = low + 1000.0
    if low > high or (low == high and (low_exclusive or high_exclusive)):
        ctx.warn(f"{path_string(path)}: numeric bounds [{low}, {high}]"
                 f" (exclusive_low={low_exclusive}, exclusive_high={high_exclusive})"
                 " cannot be satisfied; emitted the lower bound")
        low_exclusive = high_exclusive = False
        high = low
    multiple = _as_float(schema.get("multipleOf"))
    if multiple is not None and multiple <= 0:
        ctx.warn(f"{path_string(path)}: multipleOf {multiple} is not positive; ignored")
        multiple = None
    return low, high, low_exclusive, high_exclusive, multiple


def _gen_number(schema: Mapping, ctx: _SchemaCtx, path: Tuple, integer: bool) -> Any:
    low, high, low_exclusive, high_exclusive, multiple = _number_bounds(schema, ctx, path)
    if integer:
        low_index = _ceil_int(low)
        high_index = _floor_int(high)
        if low_exclusive and low_index <= low:
            low_index += 1
        if high_exclusive and high_index >= high:
            high_index -= 1
        if low_index > high_index:
            ctx.warn(f"{path_string(path)}: no integer fits the bounds; emitted"
                     f" {low_index}")
            return low_index
        if multiple is not None:
            step = _as_int(multiple)
            if step is None or step <= 0 or float(step) != multiple:
                ctx.warn(f"{path_string(path)}: multipleOf {multiple} cannot be met by"
                         " an integer; ignored it")
                return ctx.int(low_index, high_index)
            first = _ceil_int(low_index / step)
            last = _floor_int(high_index / step)
            if first > last:
                ctx.warn(f"{path_string(path)}: no multiple of {step} fits the bounds;"
                         f" emitted {low_index}")
                return low_index
            return ctx.int(first, last) * step
        return ctx.int(low_index, high_index)

    if multiple is not None:
        first = _ceil_int(low / multiple)
        last = _floor_int(high / multiple)
        if low_exclusive and first * multiple <= low:
            first += 1
        if high_exclusive and last * multiple >= high:
            last -= 1
        if first <= last:
            # 用 Decimal 相乘，避免 3 * 0.1 = 0.30000000000000004 这类浮点误差
            return float(Decimal(str(multiple)) * ctx.int(first, last))
        ctx.warn(f"{path_string(path)}: no multiple of {multiple} fits the bounds")
    span = high - low
    digits = 6
    while digits < 17 and span < 10.0 ** (-digits):
        digits += 1  # 区间极窄时提高小数位，否则四舍五入会把值压到端点上
    for _ in range(64):
        candidate = round(low + span * ctx.random(), digits)
        if (candidate > low if low_exclusive else candidate >= low) and \
           (candidate < high if high_exclusive else candidate <= high):
            return candidate
    ctx.warn(f"{path_string(path)}: could not draw a number strictly inside"
             f" [{low}, {high}] (exclusive_low={low_exclusive},"
             f" exclusive_high={high_exclusive}); emitted the midpoint")
    return (low + high) / 2.0


def _gen_string(schema: Mapping, ctx: _SchemaCtx, path: Tuple) -> str:
    min_length = max(0, _as_int(schema.get("minLength")) or 0)
    declared_max = _as_int(schema.get("maxLength"))
    max_length = (min(declared_max, MAX_STRING_LENGTH) if declared_max is not None
                  else min(min_length + 32, MAX_STRING_LENGTH))
    if max_length < min_length:
        ctx.warn(f"{path_string(path)}: minLength {min_length} is greater than"
                 f" maxLength {declared_max}; used {min_length}")
        max_length = min_length
    pattern = schema.get("pattern")
    if isinstance(pattern, str) and pattern:
        text, reason = _expand_regex(pattern, ctx, min_length, max_length)
        if text is None:
            ctx.warn(f"{path_string(path)}: pattern '{pattern}' is not generatable"
                     f" ({reason}); emitted a placeholder string")
            return _pattern_placeholder(pattern, min_length, max_length)
        if not min_length <= len(text) <= max_length:
            ctx.warn(f"{path_string(path)}: pattern '{pattern}' produced {len(text)}"
                     f" chars, outside minLength/maxLength [{min_length}, {max_length}]")
        return text
    declared_format = schema.get("format")
    if isinstance(declared_format, str) and declared_format.lower() in _FORMAT_KINDS:
        text = _mock_value(_FORMAT_KINDS[declared_format.lower()], ctx)
        if not isinstance(text, str):
            text = str(text)
        if len(text) < min_length or (declared_max is not None and len(text) > declared_max):
            ctx.warn(f"{path_string(path)}: format '{declared_format}' produced"
                     f" {len(text)} chars, outside minLength/maxLength")
        return text
    length = ctx.int(min_length, max_length)
    if ctx.locale == "zh":
        return _locale_text(ctx, length)
    return _token(ctx, length, length)


def _pattern_placeholder(pattern: str, min_length: int, max_length: int) -> str:
    """正则不可生成时的占位串：保留字面字符，再补齐/裁剪到长度约束。"""
    literals = "".join(ch for ch in pattern if ch.isalnum() or ch in " _-.")
    text = literals or "example"
    if len(text) < min_length:
        text += "a" * (min_length - len(text))
    return text[:max_length] if max_length < len(text) else text


def _gen_object(schema: Mapping, ctx: _SchemaCtx, depth: int, path: Tuple) -> Dict[str, Any]:
    properties = schema.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}
    required_raw = schema.get("required")
    required = [k for k in required_raw if isinstance(k, str)] \
        if isinstance(required_raw, (list, tuple)) else []
    keys: List[str] = [str(k) for k in properties.keys()]
    for key in required:
        if key not in keys:
            keys.append(key)
            ctx.warn(f"{path_string(path)}: required property '{key}' has no entry in"
                     " 'properties'; emitted a generic string for it")
    max_properties = _as_int(schema.get("maxProperties"))
    if max_properties is not None and 0 <= max_properties < len(keys):
        preferred = [k for k in keys if k in required] + [k for k in keys if k not in required]
        keys = preferred[:max_properties]
        ctx.warn(f"{path_string(path)}: maxProperties {max_properties} is smaller than"
                 f" the {len(preferred)} declared properties; kept {', '.join(keys) or 'none'}")
        if any(key not in keys for key in required):
            ctx.warn(f"{path_string(path)}: maxProperties conflicts with 'required';"
                     " the generated object cannot satisfy both")
    record: Dict[str, Any] = {}
    for key in keys:
        sub = properties[key] if key in properties else True
        record[key] = _schema_value(sub, ctx, depth + 1, path + ("properties", key))
    min_properties = _as_int(schema.get("minProperties"))
    if min_properties is not None and len(record) < min_properties:
        if schema.get("additionalProperties") is False:
            ctx.warn(f"{path_string(path)}: minProperties {min_properties} cannot be met"
                     " because additionalProperties is false")
        else:
            extra = schema.get("additionalProperties")
            extra_schema: Any = extra if isinstance(extra, (Mapping, bool)) else True
            index = 1
            while len(record) < min_properties and index <= min_properties + 64:
                key = f"prop{index}"
                index += 1
                if key in record:
                    continue
                record[key] = _schema_value(extra_schema, ctx, depth + 1,
                                            path + ("additionalProperties",))
    return record


def _gen_array(schema: Mapping, ctx: _SchemaCtx, depth: int, path: Tuple) -> List[Any]:
    prefix_raw = schema.get("prefixItems")
    items = schema.get("items", {"type": "string"})
    if not isinstance(prefix_raw, (list, tuple)):
        if isinstance(items, (list, tuple)):  # draft-04 的元组形式
            prefix_raw = items
            items = schema.get("additionalItems", {"type": "string"})
        else:
            prefix_raw = []
    prefixes = list(prefix_raw)
    min_items = _as_int(schema.get("minItems"))
    max_items = _as_int(schema.get("maxItems"))
    target = ctx.array_items
    if min_items is not None:
        target = max(target, min_items)
    if max_items is not None:
        target = min(target, max_items)
    if min_items is not None and max_items is not None and min_items > max_items:
        ctx.warn(f"{path_string(path)}: minItems {min_items} is greater than maxItems"
                 f" {max_items}; used {min_items}")
        target = min_items
    target = max(0, min(target, MAX_ARRAY_LENGTH))

    values: List[Any] = []
    for index, sub in enumerate(prefixes):
        if len(values) >= target:
            ctx.warn(f"{path_string(path)}: maxItems {max_items} is smaller than the"
                     f" {len(prefixes)} prefixItems; truncated the array")
            break
        values.append(_schema_value(sub, ctx, depth + 1, path + ("prefixItems", index)))
    item_depth = depth + 1
    if items is False:
        if len(values) < target:
            ctx.warn(f"{path_string(path)}: items is false, so the array cannot hold the"
                     f" {target} items required by minItems")
    else:
        while len(values) < target:
            values.append(_schema_value(items, ctx, item_depth, path + ("items",)))
    if schema.get("uniqueItems"):
        values = _unique_items(values, items, ctx, item_depth, target, path)
    return values


def _can_disambiguate(schema: Any) -> str:
    """判断能否安全地人为构造互不相同的条目（用于 ``uniqueItems`` 兜底）。

    只要 item schema 带任何长度/数值/取值约束，人为造差就可能把值推出约束范围，
    这时宁可少生成几个元素并告警，也不产出违反 schema 的数据。
    """
    if isinstance(schema, bool):
        return "string" if schema else ""
    if not isinstance(schema, Mapping):
        return ""
    constrained = (
        "enum", "const", "pattern", "format", "$ref", "properties", "items",
        "prefixItems", "allOf", "anyOf", "oneOf", "minLength", "maxLength",
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    )
    if any(key in schema for key in constrained):
        return ""
    declared = schema.get("type")
    if isinstance(declared, (list, tuple)):
        declared = declared[0] if declared else "string"
    if declared in ("string", "integer", "number"):
        return str(declared)
    return ""


def _unique_items(values: List[Any], items: Any, ctx: _SchemaCtx, depth: int,
                  target: int, path: Tuple) -> List[Any]:
    """按 ``uniqueItems`` 去重；不够时重试生成，最后才考虑人为造差。"""
    result: List[Any] = []
    seen = set()
    for value in values:
        key = _canonical(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    attempts = 0
    budget = target * 10 + 50
    while len(result) < target and attempts < budget:
        attempts += 1
        candidate = _schema_value(items, ctx, depth, path + ("items",))
        key = _canonical(candidate)
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    if len(result) >= target:
        return result
    kind = _can_disambiguate(items)
    if kind in ("string", "integer", "number"):
        counter = 0
        cap = target * 100 + 1000
        while len(result) < target and counter < cap:
            counter += 1
            if kind == "string":
                candidate: Any = f"{_mock_value('word', ctx)}-{counter}"
            else:
                base = target * 1000 + counter
                candidate = base if kind == "integer" else float(base)
            key = _canonical(candidate)
            if key in seen:
                continue
            seen.add(key)
            result.append(candidate)
        if len(result) >= target:
            return result
    ctx.warn(f"{path_string(path)}: uniqueItems needs {target} distinct values but the"
             " item schema cannot produce enough; emitted fewer items")
    return result


def _branch_terminates(schema: Any) -> bool:
    """分支能否在**不再往下展开**的前提下产出合法值（用于深度上限处选分支）。"""
    if schema is True or schema is False:
        return True
    if not isinstance(schema, Mapping) or "$ref" in schema:
        return False
    if "const" in schema:
        return True
    enum = schema.get("enum")
    if isinstance(enum, (list, tuple)) and enum:
        return True
    if any(key in schema for key in ("properties", "items", "prefixItems", "allOf",
                                     "anyOf", "oneOf")):
        return False
    declared = schema.get("type")
    if isinstance(declared, str):
        declared = [declared]
    if not isinstance(declared, (list, tuple)) or not declared:
        return False
    types = [item for item in declared if isinstance(item, str)]
    return bool(types) and all(item in _SCALAR_TYPES for item in types)


_TERMINAL_BUDGET = 3


def _terminal(schema: Mapping, ctx: _SchemaCtx, path: Tuple,
              budget: int = _TERMINAL_BUDGET) -> Any:
    """深度上限处的收尾：优先挑一个**可终止**的分支，尽量不破坏 schema 约束。

    ``budget`` 限制这里对 schema 结构的再展开层数（只走 ``required`` /
    ``minItems`` 这些"必须满足"的部分），因此即使 schema 本身嵌套很深也不会
    把 Python 调用栈打爆。
    """
    where = path_string(path)
    ctx.warn(f"{where}: depth limit {ctx.max_depth} reached; emitted a terminal value")
    if "const" in schema:
        return _clone_value(schema["const"])
    enum = schema.get("enum")
    if isinstance(enum, (list, tuple)) and enum:
        return _clone_value(ctx.choice(enum))
    candidates: List[Any] = [schema]
    for keyword in ("anyOf", "oneOf"):
        branches = schema.get(keyword)
        if isinstance(branches, (list, tuple)):
            candidates.extend(branches)
    for candidate in candidates:
        if candidate is False:
            return None
        if candidate is True:
            return _mock_value("word", ctx)
        if not isinstance(candidate, Mapping) or "$ref" in candidate:
            continue
        if "const" in candidate:
            return _clone_value(candidate["const"])
        branch_enum = candidate.get("enum")
        if isinstance(branch_enum, (list, tuple)) and branch_enum:
            return _clone_value(ctx.choice(branch_enum))
        types = _supported_types(candidate, ctx, path) or _infer_types(candidate)
        scalar = [item for item in types if item in _SCALAR_TYPES]
        if not scalar:
            continue
        kind = scalar[0]
        if kind == "null":
            return None
        if kind == "boolean":
            return bool(ctx.rng.randint(0, 1))
        if kind == "string":
            return _gen_string(candidate, ctx, path)
        return _gen_number(candidate, ctx, path, kind == "integer")

    types = _supported_types(schema, ctx, path) or _infer_types(schema)
    kind = types[0] if types else "object"
    if budget > 0:
        if kind == "array":
            items = _terminal_items(schema, ctx, path, budget)
            if items is not None:
                return items
        elif kind == "object":
            record = _terminal_object(schema, ctx, path, budget)
            if record is not None:
                return record
    if kind == "array":
        ctx.warn(f"{where}: emitted an empty array; 'minItems' may be violated")
        return []
    if kind == "object":
        ctx.warn(f"{where}: emitted an empty object; 'required' may be violated")
        return {}
    return None


def _terminal_items(schema: Mapping, ctx: _SchemaCtx, path: Tuple,
                    budget: int) -> Optional[List[Any]]:
    """深度上限处的数组：按 ``prefixItems`` / ``minItems`` 造出最少的合法元素。"""
    prefixes = schema.get("prefixItems")
    items = schema.get("items")
    if isinstance(items, (list, tuple)) and not isinstance(prefixes, (list, tuple)):
        prefixes = items
        items = None
    min_items = _as_int(schema.get("minItems")) or 0
    if not isinstance(prefixes, (list, tuple)) and min_items <= 0:
        return None
    if items is False and min_items > len(prefixes or ()):
        return None
    item_schema: Any = items if isinstance(items, (Mapping, bool)) else True
    values: List[Any] = []
    for index, sub in enumerate(prefixes or ()):
        values.append(_terminal(sub, ctx, path + ("prefixItems", index), budget - 1)
                      if isinstance(sub, Mapping) else _mock_value("word", ctx))
    while len(values) < min_items and len(values) < MAX_ARRAY_LENGTH:
        values.append(_terminal(item_schema, ctx, path + ("items",), budget - 1)
                      if isinstance(item_schema, Mapping) else _mock_value("word", ctx))
    return values


def _terminal_object(schema: Mapping, ctx: _SchemaCtx, path: Tuple,
                     budget: int) -> Optional[Dict[str, Any]]:
    """深度上限处的对象：至少把 ``required`` 与 ``minProperties`` 满足掉。"""
    properties = schema.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}
    required_raw = schema.get("required")
    required = [k for k in required_raw if isinstance(k, str)] \
        if isinstance(required_raw, (list, tuple)) else []
    min_properties = _as_int(schema.get("minProperties")) or 0
    if not required and min_properties <= 0:
        return None
    need = max(len(required), min_properties)
    keys: List[str] = list(required)
    for key in properties:
        if len(keys) >= need:
            break
        name = str(key)
        if name not in keys:
            keys.append(name)
    if len(keys) < need:
        return None  # 可用键不够（例如 additionalProperties: false），交给上层告警
    record: Dict[str, Any] = {}
    for key in keys:
        sub = properties.get(key, True)
        record[key] = (_terminal(sub, ctx, path + ("properties", key), budget - 1)
                       if isinstance(sub, Mapping) else _mock_value("word", ctx))
    return record


def _schema_value(schema: Any, ctx: _SchemaCtx, depth: int, path: Tuple) -> Any:
    """按 schema 生成一个值；``depth`` 用于保证自引用 ``$ref`` 一定终止。"""
    if schema is False:
        ctx.warn(f"{path_string(path)}: schema is `false`, no value can satisfy it;"
                 " emitted null")
        return None
    if schema is True or schema is None:
        return _mock_value("word", ctx)
    if not isinstance(schema, Mapping):
        ctx.warn(f"{path_string(path)}: schema fragment is not an object or boolean;"
                 " emitted an empty object")
        return {}

    # $ref / allOf 组合展开（有界，避免互相引用导致死循环）
    for _ in range(_MAX_REF_HOPS):
        if isinstance(schema.get("$ref"), str):
            schema = _resolve_ref(schema, ctx, path)
            depth += 1
            if not isinstance(schema, Mapping):
                return {}
            continue
        if schema.get("allOf") is not None:
            schema = _merge_all_of(schema, ctx, depth, path)
            depth += 1
            continue
        break
    for keyword in _UNSUPPORTED_KEYWORDS:
        if keyword in schema:
            ctx.warn(f"{path_string(path)}: '{keyword}' is not enforced by the generator;"
                     " the value may violate it")
    if "const" in schema:
        return _clone_value(schema["const"])
    enum = schema.get("enum")
    if isinstance(enum, (list, tuple)) and enum:
        return _clone_value(ctx.choice(enum))
    if isinstance(enum, (list, tuple)):
        ctx.warn(f"{path_string(path)}: 'enum' is empty; ignored it")

    examples = schema.get("examples")
    if isinstance(examples, (list, tuple)) and examples:
        candidate = examples[0]
        if _fits(candidate, schema):
            return _clone_value(candidate)
        ctx.warn(f"{path_string(path)}: examples[0] does not satisfy its own schema;"
                 " generated a value instead")
    if "default" in schema:
        candidate = schema["default"]
        if _fits(candidate, schema):
            return _clone_value(candidate)
        if candidate is not None:
            # "default": null 是常见写法，静默忽略即可，不必刷屏
            ctx.warn(f"{path_string(path)}: 'default' does not satisfy its own schema;"
                     " generated a value instead")

    for keyword in ("anyOf", "oneOf"):
        branches = schema.get(keyword)
        if isinstance(branches, (list, tuple)) and branches:
            if keyword == "oneOf" and len(branches) > 1:
                ctx.warn(f"{path_string(path)}: oneOf has {len(branches)} branches;"
                         " picked the first, which may also satisfy the others")
            index = 0
            if depth + 1 >= ctx.max_depth and len(branches) > 1:
                # 快到深度上限了：优先挑一个可终止的分支，否则自引用 schema 只能
                # 产出空容器并违反 required
                for candidate_index, branch in enumerate(branches):
                    if _branch_terminates(branch):
                        index = candidate_index
                        if index:
                            ctx.warn(f"{path_string(path)}: depth limit {ctx.max_depth}"
                                     f" is about to be reached, so {keyword} branch"
                                     f" {index} was used instead of branch 0")
                        break
            return _schema_value(branches[index], ctx, depth + 1,
                                 path + (keyword, index))

    if depth >= ctx.max_depth:
        return _terminal(schema, ctx, path)

    types = _supported_types(schema, ctx, path) or _infer_types(schema)
    if depth + 1 >= ctx.max_depth:
        # 快到深度上限了，能选标量就选标量，避免下一层被迫产出空容器
        scalar = [item for item in types if item in _SCALAR_TYPES]
        if scalar:
            types = scalar
    kind = types[0] if types else "object"
    if kind == "object":
        return _gen_object(schema, ctx, depth, path)
    if kind == "array":
        return _gen_array(schema, ctx, depth, path)
    if kind == "string":
        return _gen_string(schema, ctx, path)
    if kind in ("number", "integer"):
        return _gen_number(schema, ctx, path, kind == "integer")
    if kind == "boolean":
        return bool(ctx.rng.randint(0, 1))
    if kind == "null":
        return None
    return {}  # pragma: no cover - _infer_types 只返回受支持的类型


def generate_from_schema(schema: Any, *, options: Optional[MockOptions] = None,
                         max_depth: int = 6,
                         array_items: int = 2) -> Tuple[Any, List[str]]:
    """按 JSON Schema 生成一个实例，返回 ``(value, warnings)``。

    支持的关键字：``type``（含类型数组）、``enum``、``const``、``required``、
    ``properties``、``additionalProperties``、``minProperties``/``maxProperties``、
    ``items``/``prefixItems``、``minItems``/``maxItems``/``uniqueItems``、
    ``minimum``/``maximum``/``exclusiveMinimum``/``exclusiveMaximum``/``multipleOf``、
    ``minLength``/``maxLength``/``pattern``/``format``、``default``、``examples``、
    ``allOf``（浅合并）、``anyOf``/``oneOf``（取第一个分支）、本地 ``$ref``。

    深度到 ``max_depth`` 后走 :func:`_terminal`：优先挑 ``anyOf``/``oneOf`` 里可终止
    的分支（例如 ``{"type":"null"}``），因此自引用 ``$ref`` 一定终止；确实无法终止时
    会产出空容器并给出"可能违反 required/minItems"的警告。远程 ``$ref``、不支持的
    正则与 ``not``/``if`` 等关键字都只告警，不抛异常。
    """
    try:
        depth_limit = int(max_depth)
    except (TypeError, ValueError):
        depth_limit = 6
    depth_limit = max(1, min(depth_limit, MAX_SCHEMA_DEPTH))
    try:
        item_count = int(array_items)
    except (TypeError, ValueError):
        item_count = 2
    item_count = max(0, min(item_count, MAX_ARRAY_LENGTH))
    ctx = _SchemaCtx(options, schema, depth_limit, item_count)
    try:
        value = _schema_value(schema, ctx, 0, ())
    except RecursionError:  # pragma: no cover - 深度已被 max_depth 夹住
        ctx.warn("schema nesting is too deep; generation stopped early")
        value = None
    except Exception as exc:  # pragma: no cover - 防御：schema 再离谱也不能崩
        ctx.warn(f"generation failed: {type(exc).__name__}: {exc}")
        value = None
    return value, ctx.warnings


# --------------------------------------------------------------------------- #
# 正则子集展开（schema 的 pattern）
# --------------------------------------------------------------------------- #
_CLASS_DIGITS = _string.digits
_CLASS_WORD = _string.ascii_letters + _string.digits + "_"
_CLASS_NON_WORD = " -+@#$%&=/:;,.!?"
_CLASS_SPACE = " "
_CLASS_NON_SPACE = _string.ascii_letters + _string.digits
_CLASS_DOT = _string.ascii_letters + _string.digits + " -_.,;:"
_SAFE_ALPHABET = _string.ascii_letters + _string.digits + " -_.,;:@#"
_SIMPLE_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "f": "\f", "v": "\v", "0": "\0"}
_SHORTHAND = {
    "d": _CLASS_DIGITS, "D": _CLASS_NON_SPACE, "w": _CLASS_WORD, "W": _CLASS_NON_WORD,
    "s": _CLASS_SPACE, "S": _CLASS_NON_SPACE,
}


class _RegexError(Exception):
    """正则子集解析失败（内部信号，绝不越过公共 API 边界）。"""


def _parse_char_class(text: str, index: int) -> Tuple[str, int]:
    """解析 ``[...]``，返回 (候选字符集, 下一个位置)。``index`` 指向 ``[``。"""
    index += 1
    negate = False
    if index < len(text) and text[index] == "^":
        negate = True
        index += 1
    chars = set()
    first = True
    while index < len(text):
        ch = text[index]
        if ch == "]" and not first:
            index += 1
            break
        first = False
        if ch == "\\":
            index += 1
            if index >= len(text):
                raise _RegexError("truncated escape in character class")
            esc = text[index]
            if esc in _SHORTHAND:
                chars.update(_SHORTHAND[esc])
            elif esc in _SIMPLE_ESCAPES:
                chars.add(_SIMPLE_ESCAPES[esc])
            else:
                chars.add(esc)
            index += 1
            continue
        if (index + 2 < len(text) and text[index + 1] == "-"
                and text[index + 2] != "]" and ord(text[index + 2]) >= ord(ch)):
            for code in range(ord(ch), ord(text[index + 2]) + 1):
                if 0x20 <= code <= 0x7E:
                    chars.add(chr(code))
            index += 3
            continue
        if 0x20 <= ord(ch) <= 0x7E:
            chars.add(ch)
        index += 1
    else:
        raise _RegexError("unterminated character class")
    if not chars:
        raise _RegexError("empty character class")
    if negate:
        chars = {c for c in _SAFE_ALPHABET if c not in chars}
        if not chars:
            raise _RegexError("negated character class matches nothing printable")
    return "".join(sorted(chars)), index


def _parse_atom(text: str, index: int, depth: int) -> Tuple[Tuple, int]:
    """解析一个原子（字面量 / 字符集 / 分组），返回 (token, 下一个位置)。"""
    ch = text[index]
    if ch == "(":
        if depth >= _MAX_REGEX_GROUP_DEPTH:
            raise _RegexError("group nesting deeper than "
                              f"{_MAX_REGEX_GROUP_DEPTH} is not supported")
        index += 1
        if index < len(text) and text[index] == "?":
            flag = text[index + 1 : index + 2]
            if flag == ":":
                index += 2
            elif flag in ("=", "!", "<", ">", "#", "P"):
                raise _RegexError("look-around, named groups and comments are not"
                                  " supported")
            else:
                raise _RegexError(f"unsupported group flag '(?{flag}'")
        branches: List[List[Tuple]] = []
        current: List[Tuple] = []
        while True:
            if index >= len(text):
                raise _RegexError("unterminated group")
            if text[index] == ")":
                index += 1
                break
            if text[index] == "|":
                branches.append(current)
                current = []
                index += 1
                continue
            token, index = _parse_atom_quantified(text, index, depth + 1)
            if token[0] != "empty":  # 锚点不产出字符，也不能进 token 列表（长度不同）
                current.append(token)
        branches.append(current)
        return ("group", branches), index
    if ch == "[":
        charset, index = _parse_char_class(text, index)
        return ("chars", charset), index
    if ch == "\\":
        index += 1
        if index >= len(text):
            raise _RegexError("truncated escape")
        esc = text[index]
        if esc in "bBAZz":
            if esc in "bB":
                raise _RegexError("word boundary \\b is not supported")
            return ("empty", ""), index + 1
        if esc in "123456789":
            raise _RegexError("backreferences are not supported")
        if esc in _SHORTHAND:
            return ("chars", _SHORTHAND[esc]), index + 1
        if esc in _SIMPLE_ESCAPES:
            return ("chars", _SIMPLE_ESCAPES[esc]), index + 1
        if esc == "x" and index + 2 < len(text):
            try:
                return ("chars", chr(int(text[index + 1 : index + 3], 16))), index + 3
            except ValueError:
                raise _RegexError("invalid \\x escape") from None
        if esc == "u" and index + 4 < len(text):
            try:
                return ("chars", chr(int(text[index + 1 : index + 5], 16))), index + 5
            except ValueError:
                raise _RegexError("invalid \\u escape") from None
        return ("chars", esc), index + 1
    if ch == ".":
        return ("chars", _CLASS_DOT), index + 1
    if ch in "^$":
        return ("empty", ""), index + 1
    return ("chars", ch), index + 1


def _parse_quantifier(text: str, index: int) -> Tuple[int, int, bool, int]:
    """解析量词，返回 (最小次数, 最大次数, 是否开放式, 下一个位置)。"""
    if index >= len(text):
        return 1, 1, False, index
    ch = text[index]
    low = high = 1
    open_ended = False
    if ch == "*":
        low, high, open_ended, index = 0, 3, True, index + 1
    elif ch == "+":
        low, high, open_ended, index = 1, 4, True, index + 1
    elif ch == "?":
        low, high, index = 0, 1, index + 1
    elif ch == "{":
        end = text.find("}", index)
        if end < 0:
            raise _RegexError("unterminated '{' quantifier")
        body = text[index + 1 : end]
        index = end + 1
        if "," in body:
            left, _, right = body.partition(",")
            low = int(left) if left.strip() else 0
            if right.strip():
                high = int(right)
            else:
                high = low + 3
                open_ended = True
        else:
            low = high = int(body)
        if low < 0 or high < low:
            raise _RegexError(f"invalid quantifier '{{{body}}}'")
        if high > _MAX_REGEX_QUANTIFIER:
            raise _RegexError(f"quantifier larger than {_MAX_REGEX_QUANTIFIER} is not"
                              " supported")
    if index < len(text) and text[index] in "?+":  # 惰性/占有标记，对生成没有影响
        index += 1
    return low, high, open_ended, index


def _parse_atom_quantified(text: str, index: int, depth: int) -> Tuple[Tuple, int]:
    token, index = _parse_atom(text, index, depth)
    low, high, open_ended, index = _parse_quantifier(text, index)
    if token[0] == "empty":
        return token, index
    return (token[0], token[1], low, high, open_ended), index


def _compile_regex_subset(pattern: str) -> List[Tuple]:
    """把正则解析成 token 列表；不支持的构造抛 :class:`_RegexError`。"""
    branches: List[List[Tuple]] = []
    current: List[Tuple] = []
    index = 0
    length = len(pattern)
    while index < length:
        ch = pattern[index]
        if ch == "|":
            branches.append(current)
            current = []
            index += 1
            continue
        if ch == ")":
            raise _RegexError("unbalanced ')'")
        token, index = _parse_atom_quantified(pattern, index, 0)
        if token[0] != "empty":
            current.append(token)
    branches.append(current)
    if len(branches) == 1:
        return branches[0]
    return [("group", branches, 1, 1, False)]


def _expand_tokens(tokens: Sequence[Tuple], ctx: _Ctx, state: List[int]) -> List[str]:
    """展开 token 列表。``state`` = [剩余预算, 还差多少字符才到 minLength]。

    开放式量词（``*`` ``+`` ``{n,}``）会被拉长以满足 ``minLength``：同一层里最后
    一个开放量词负责补齐全部缺口，其余开放量词只按比例放宽上限。闭合量词不会被
    预算压缩（否则正则本身就匹配不上了），预算耗尽时直接停止后续展开。
    """
    out: List[str] = []
    open_left = sum(1 for token in tokens if token[4])
    for token in tokens:
        if state[0] <= 0:
            break
        kind, payload, low, high, open_ended = token
        if open_ended:
            open_left -= 1
            if open_left <= 0:
                low = max(low, state[1])
                high = max(high, low)
            else:
                high = max(high, min(state[1], _MAX_REGEX_QUANTIFIER))
            high = min(high, max(0, state[0]))
            low = min(low, high)
        repeats = ctx.int(low, high)
        before = len(out)
        for _ in range(repeats):
            if state[0] <= 0:
                break
            if kind == "chars":
                out.append(ctx.choice(payload))
                state[0] -= 1
            else:
                out.extend(_expand_tokens(ctx.choice(payload), ctx, state))
        state[1] = max(0, state[1] - (len(out) - before))
    return out


def _expand_regex(pattern: str, ctx: _Ctx, min_length: int,
                  max_length: int) -> Tuple[Optional[str], str]:
    """按正则子集生成字符串；返回 ``(文本或 None, 失败原因)``。

    长度不满足 ``minLength``/``maxLength`` 时会重试若干次；仍然不满足就把最后
    一次结果交回调用方（由调用方告警），这样至少不会返回 ``None`` 让上层退化。
    """
    try:
        tokens = _compile_regex_subset(pattern)
    except _RegexError as exc:
        return None, str(exc)
    except (ValueError, IndexError, KeyError) as exc:  # pragma: no cover - 防御
        return None, f"cannot parse the pattern ({type(exc).__name__})"
    if not tokens:
        return "", ""
    if min_length > _MAX_PATTERN_BUDGET:
        return None, f"minLength {min_length} exceeds the {_MAX_PATTERN_BUDGET} char budget"
    budget_limit = max(min_length, min(max_length, _MAX_PATTERN_BUDGET))
    best: Optional[str] = None
    for _ in range(24):
        text = "".join(_expand_tokens(tokens, ctx, [budget_limit, min_length]))
        if min_length <= len(text) <= max_length:
            return text, ""
        if best is None:
            best = text
    return best, ""

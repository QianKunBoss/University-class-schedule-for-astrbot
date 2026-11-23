"""课表 HTML 的防御式解析。

为什么叫「防御式」：拿不到全国各校的课表样本，强智不同版本的 class/id 也不同，
所以这里**不依赖任何固定 id/class 名**，而是靠结构性特征定位：

1. 先按表头文字（星期一…星期日）找到课表；
2. 把 ``rowspan``/``colspan`` 展开成二维网格 —— 强智大量用 rowspan 合并节次，
   不重建网格会导致整行错位（这是最常见的坑）；
3. 单元格内按 ``<br>`` 与分隔线切段，同格多课拆成多条 ``Course``；
4. 字段优先读 ``<font title="老师|周次(节次)|教室">``，读不到再用启发式推断；
5. 周次支持 ``1-16周`` / ``1-8,10-16周`` / ``单周`` / ``双周``。

解析出 0 条时抛 ``ParseError``，由上层把原始 HTML 存下来供排障。
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from .errors import ParseError
from .models import Course

# ---------------- 正则 ----------------
WEEKDAY_RE = re.compile(r"(?:星期|周|礼拜)\s*([一二三四五六日天])")
WEEKDAY_MAP = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}

WEEK_RANGE_RE = re.compile(r"(\d{1,2})\s*[-~—–－至到]\s*(\d{1,2})\s*周")
WEEK_LIST_RE = re.compile(r"((?:\d{1,2}\s*[,，、]\s*)+\d{1,2})\s*周")
WEEK_SINGLE_RE = re.compile(r"(?<![\d\-~—–－至到])(\d{1,2})\s*周")

PERIOD_RANGE_RE = re.compile(r"(\d{1,2})\s*[-~—–－至到]\s*(\d{1,2})\s*节")
PERIOD_SINGLE_RE = re.compile(r"第?\s*(\d{1,2})\s*节")
BIG_PERIOD_RE = re.compile(r"第?\s*([一二三四五六七八九十])\s*大节")
CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

SEPARATOR_RE = re.compile(r"^[\s\-—–_=＝]{4,}$", re.M)
LOCATION_HINT = re.compile(
    r"(楼|室|馆|场|厅|机房|实验|中心|校区|基地|教室|[A-Za-z]\s*\d{2,4}|\d+\s*号楼)", re.I
)
TEACHER_LIKE = re.compile(r"^[\u4e00-\u9fa5·]{2,6}$")
# 表头里可能是「节次」「时间」「上午」这类非星期列
PERIOD_HEADER_HINT = re.compile(r"(节次|节|时间|时段|上午|下午|晚上|午别|大节)")

# 周次片段：1-16周 / 第3周 / 1-16周(单)，以及整段括号里含「周」的内容（(周次)（第1-16周））。
# 用于把「世贸楼301（第1-16周）(周次)」这种混排行拆开 —— 真实踩过的坑：
# 这类行先被当作周次整行吃掉，教室就丢了；剩下的括号碎片还会粘在教室名后面。
_WEEK_FRAG_RE = re.compile(
    r"第?\s*\d{1,2}\s*(?:[-~—–－至到]\s*\d{1,2})?\s*周(?:[（(][^)）]*[)）])?"
    r"|[（(][^)）]*周[^)）]*[)）]"
)
_EMPTY_PAREN_RE = re.compile(r"[（(]\s*[)）]")


def _strip_week_fragments(text: str) -> str:
    """把一行里的周次片段剥掉，剩下的部分如果像教室就用作 location。"""
    rest = _WEEK_FRAG_RE.sub(" ", text or "")
    rest = _EMPTY_PAREN_RE.sub("", rest)
    return _clean(rest)


def _tidy_location(text: str) -> str:
    """教室名收尾清理：去掉粘连的（周）（周次）碎片与空括号。"""
    return _strip_week_fragments(text).strip(" -—–")


def _tidy_name(text: str) -> str:
    """课程名收尾清理：去掉 (周次)（周） 这类碎片与空括号。"""
    return _strip_week_fragments(text).strip(" -—–")


# ---------------- 基础工具 ----------------
# 教务用红色标记标注调课段：<span><font color='red'>&nbspO</font></span>，
# 「&nbspO / &nbspP」是没写分号的实体残留，会混进课程名候选（真实踩过的坑）。
_JUNK_MARK_RE = re.compile(r"(?i)&\s*nbsp\S{0,2}")


def _clean(text: str) -> str:
    text = _JUNK_MARK_RE.sub("", text or "")
    return re.sub(r"[\t\r\n\u00a0\u3000 ]+", " ", text).strip()


def _cell_text(cell: Tag | None) -> str:
    """取单元格纯文本，``<br>`` 转成换行，保留行结构。"""
    if cell is None:
        return ""
    html = str(cell)
    html = re.sub(r"(?is)<br\s*/?>", "\n", html)
    txt = BeautifulSoup(html, "lxml").get_text("\n")
    lines = [_clean(ln) for ln in txt.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def _font_fields(cell: Tag) -> dict[str, str]:
    """从 ``<font title="...">`` 里取结构化字段（强智的规范写法）。"""
    out: dict[str, str] = {}
    for font in cell.find_all("font"):
        title = _clean(font.get("title") or "")
        text = _clean(font.get_text(" ", strip=True))
        if not title or not text:
            continue
        low = title.lower()
        if "老师" in title or "教师" in title or "teacher" in low:
            out.setdefault("teacher", text)
        elif "周" in title or "节" in title:
            out.setdefault("weeks", text)
        elif "教室" in title or "地点" in title or "场地" in title:
            out.setdefault("location", text)
        elif "课程" in title or "名称" in title:
            out.setdefault("name", text)
    return out


def _expand_table(table: Tag) -> tuple[list[list[Tag | None]], dict[int, tuple[int, int, int, int]]]:
    """把表格展开成二维网格，并记录每个单元格的 (首行, 首列, 行跨度, 列跨度)。"""
    rows = table.find_all("tr")
    occupied: dict[tuple[int, int], Tag] = {}
    info: dict[int, tuple[int, int, int, int]] = {}

    for r, tr in enumerate(rows):
        c = 0
        for cell in tr.find_all(["td", "th"], recursive=False):
            while (r, c) in occupied:
                c += 1
            try:
                rowspan = max(1, int(str(cell.get("rowspan") or 1)))
            except ValueError:
                rowspan = 1
            try:
                colspan = max(1, int(str(cell.get("colspan") or 1)))
            except ValueError:
                colspan = 1
            for dr in range(rowspan):
                for dc in range(colspan):
                    occupied[(r + dr, c + dc)] = cell
            info.setdefault(id(cell), (r, c, rowspan, colspan))
            c += colspan

    if not occupied:
        return [], {}
    n_rows = max(r for r, _ in occupied) + 1
    n_cols = max(c for _, c in occupied) + 1
    grid = [[occupied.get((r, c)) for c in range(n_cols)] for r in range(n_rows)]
    return grid, info


def _find_header(grid: list[list[Tag | None]], info: dict) -> tuple[int, dict[int, int]] | None:
    """找表头行并返回 (行号, {列号: 星期})。"""
    best: tuple[int, dict[int, int]] | None = None
    for r, row in enumerate(grid):
        colmap: dict[int, int] = {}
        for c, cell in enumerate(row):
            if cell is None:
                continue
            text = _cell_text(cell)
            if not text or len(text) > 8:
                continue
            m = WEEKDAY_RE.search(text)
            if m:
                colmap[c] = WEEKDAY_MAP[m.group(1)]
        if len(colmap) >= 3 and (best is None or len(colmap) > len(best[1])):
            best = (r, colmap)
    return best


def _parse_row_period(label: str, row_index: int, big_size: int) -> tuple[int, int]:
    """把「第一大节」「1-2节」「第3节」或纯数字解析成 (起, 止)。"""
    text = _clean(label)
    if text:
        m = BIG_PERIOD_RE.search(text)
        if m and m.group(1) in CN_NUM:
            idx = CN_NUM[m.group(1)]
            start = (idx - 1) * big_size + 1
            return start, start + big_size - 1
        m = PERIOD_RANGE_RE.search(text)
        if m:
            return int(m.group(1)), int(m.group(2))
        m = PERIOD_SINGLE_RE.search(text)
        if m:
            return int(m.group(1)), int(m.group(1))
        if re.fullmatch(r"\d{1,2}", text):
            n = int(text)
            return n, n
        if PERIOD_HEADER_HINT.search(text):
            # 「上午」「第一大节」之外的模糊标签，按行序推断
            pass
    start = row_index * big_size + 1
    return start, start + big_size - 1


def parse_weeks(text: str, max_week: int = 20) -> tuple[list[int], bool]:
    """解析周次。

    处理过的真实写法：
    ``1-16周`` / ``1-8,10-16周`` / ``1-16周(单)`` / ``第3周`` / ``1-16周(1-2节)``。

    两个坑：
    * ``1-8,10-16周`` 只有最后一段带「周」字，如果按整串正则匹配，前半段会丢失；
    * ``1-16周(1-2节)`` 里的 ``1-2`` 是节次不是周次，必须先去掉含「节」的括号。

    Returns:
        (周次列表, 是否为猜的)。猜的情况下上层会标记 ``weeks_guessed``。
    """
    t = (text or "").replace(" ", "").replace("\u3000", "")
    if not t:
        return [], True

    # 去掉「(1-2节)」「（第3-4节）」这类节次括号，避免把节次当成周次
    t = re.sub(r"[（(][^)）]*节[^)）]*[)）]", "", t)
    if "周" not in t:
        return [], True

    weeks: set[int] = set()
    for chunk in re.split(r"[,，、;；]", t):
        chunk = chunk.strip()
        if not chunk:
            continue
        # 只解析「周」之前的那一段，例如 "1-16周(单)" -> "1-16"
        head = chunk.split("周", 1)[0]
        if not head:
            continue
        ranges = re.findall(r"(\d{1,2})\s*[-~—–－至到]\s*(\d{1,2})", head)
        if ranges:
            for a, b in ranges:
                a, b = int(a), int(b)
                if 1 <= a <= b:
                    weeks.update(range(a, min(b, max_week) + 1))
            continue
        for n in re.findall(r"\d{1,2}", head):
            v = int(n)
            if 1 <= v <= max_week:
                weeks.add(v)

    odd = "单周" in t or bool(re.search(r"[（(]单[）)]", t))
    even = "双周" in t or bool(re.search(r"[（(]双[）)]", t))
    if odd:
        weeks = {w for w in weeks if w % 2 == 1}
    if even:
        weeks = {w for w in weeks if w % 2 == 0}

    if not weeks:
        return [], True
    return sorted(weeks), False


def parse_periods_from_text(text: str, big_size: int) -> tuple[int, int] | None:
    """从「1-16周(1-2节)」这类文本里单独抽节次。"""
    t = _clean(text)
    m = BIG_PERIOD_RE.search(t)
    if m and m.group(1) in CN_NUM:
        idx = CN_NUM[m.group(1)]
        start = (idx - 1) * big_size + 1
        return start, start + big_size - 1
    m = PERIOD_RANGE_RE.search(t)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = PERIOD_SINGLE_RE.search(t)
    if m:
        return int(m.group(1)), int(m.group(1))
    return None


# ---------------- 单元格解析 ----------------
def _classes_of(d: Tag) -> set[str]:
    return {str(c).strip().lower() for c in (d.get("class") or [])}


def _is_kbcontent(d: Tag) -> bool:
    return any(c.startswith("kbcontent") for c in _classes_of(d))


def _is_full_kbcontent(d: Tag) -> bool:
    """``class="kbcontent"``（完整版，含教师）；kbcontent1/2 是简版（琼台等学校）。"""
    classes = _classes_of(d)
    return "kbcontent" in classes and not any(c.startswith("kbcontent") and c != "kbcontent" for c in classes)


def _split_blocks(cell: Tag) -> list[Tag]:
    """同格多课：取全部 ``div.kbcontent*``，没有就整个 cell。

    注意不能只取「完整版」：kbcontent1/kbcontent 也可能是**不同课程**
    （测试夹具里就是 大学英语 + 体育），两个都要解析，
    冲突由 parse_timetable 末尾的去重（保留信息更全的一份）解决。
    """
    divs = [d for d in cell.find_all("div") if _is_kbcontent(d)]
    if not divs:
        return [cell]
    return divs


def _split_entry_segments(block: Tag) -> list[Tag]:
    """把一个 block 按 ``------`` 分隔线拆成多个子段，**保留 ``<font>`` 结构**。

    琼台的完整块里，同一门课的不同周次段（2(周) / 3(周) / 7-16(周)）用分隔线隔开，
    每段都自带 ``<font title='老师|周次(节次)|教室'>``。若先把整块转成纯文本再拆，
    font 的结构化字段就丢了 —— 旧逻辑因此只保留了第一段的周次，课程大量缺失
    （真实踩过的坑）。
    """
    groups: list[list] = []
    cur: list = []
    for child in block.children:
        text = child if isinstance(child, str) else child.get_text(" ", strip=True)
        if text and SEPARATOR_RE.fullmatch(text.strip()):
            if cur:
                groups.append(cur)
                cur = []
            continue
        cur.append(child)
    if cur:
        groups.append(cur)
    if not groups:
        return [block]

    out: list[Tag] = []
    for g in groups:
        inner = "".join(str(x) for x in g)
        soup = BeautifulSoup(f"<div>{inner}</div>", "lxml")
        if soup.div is not None:
            out.append(soup.div)
    return out


def _classify_lines(lines: list[str], known: dict[str, str], default_name: str = "") -> dict[str, str]:
    """把单元格的几行文字归类到 课程名/教师/周次/教室。

    分两趟做，不能边遍历边定教师 —— 否则「数据结构」这种 2~6 个汉字的课程名
    会被 ``TEACHER_LIKE`` 先匹配成教师（真实踩过的坑）。
    规则：先摘出周次与教室，剩下的最长一行是课程名，再在剩余里找最短的中文串当教师。

    ``default_name``：同格多课时，后续块常常**省略课程名**（只写 教师/周次/教室），
    这时沿用上一块的名字（真实踩过的坑：否则教室行会被当成课程名）。
    """
    teacher = known.get("teacher", "")
    weeks = known.get("weeks", "")
    location = known.get("location", "")
    name = known.get("name", "")

    leftovers: list[str] = []
    for ln in lines:
        if not ln:
            continue
        if ln in {teacher, weeks, location}:
            continue
        has_week_num = "周" in ln and bool(re.search(r"\d", ln))
        if has_week_num and not weeks:
            weeks = ln
            # 混排行：教室和周次写在同一行（如「世贸楼301(第1-16周)」）
            # —— 把周次剥掉，剩下的像教室就同时用作 location
            rest = _strip_week_fragments(ln)
            if rest and LOCATION_HINT.search(rest) and not location:
                location = rest
            continue
        if not location:
            if LOCATION_HINT.search(ln) and "周" not in ln:
                location = ln
                continue
            if "周" in ln and not re.search(r"\d", ln):
                # 「厚德楼（周次）」这类：教室后面粘了不含数字的周次括号
                rest = _strip_week_fragments(ln)
                if rest and LOCATION_HINT.search(rest):
                    location = rest
                    continue
        leftovers.append(ln)

    if not name:
        # 名字兜底：先排除分隔线与明显是教室的行；都不行再用 default_name；
        # 最后才允许「矮子里面拔将军」（旧行为），避免出现名字是「----」或教室名。
        cands = [x for x in leftovers if not SEPARATOR_RE.fullmatch(x) and not LOCATION_HINT.search(x)]
        if len(cands) == 1 and TEACHER_LIKE.fullmatch(cands[0]) and default_name:
            # 续段只写了教师名 → 课程名沿用上一段
            name = default_name
        elif cands:
            # 优先「不像教师名」的候选；全员都像教师名时取**最先出现**的
            # （强智格式里第一行是课程名；「体育/冯功辉」不能选成教师）
            non_teacher = [x for x in cands if not TEACHER_LIKE.fullmatch(x)]
            if non_teacher:
                name = non_teacher[0] if len(non_teacher) == 1 else max(non_teacher, key=len)
            else:
                name = cands[0]
        elif default_name:
            name = default_name
        elif leftovers:
            name = max(leftovers, key=len)
        if name:
            leftovers = [x for x in leftovers if x != name]

    if not teacher and leftovers:
        for ln in sorted(leftovers, key=len):
            if TEACHER_LIKE.fullmatch(ln):
                teacher = ln
                break

    return {
        "name": _tidy_name(name),
        "teacher": teacher,
        "weeks": weeks,
        "location": _tidy_location(location),
    }


def _parse_cell(
    cell: Tag,
    weekday: int,
    start_period: int,
    end_period: int,
    max_week: int,
    big_size: int,
) -> list[Course]:
    out: list[Course] = []
    last_name = ""  # 同一块内被分隔线隔开的续段会省略课程名，沿用上一段的名字
    for block in _split_blocks(cell):
        # 按分隔线拆成子段（保留 <font> 结构），每段独立取字段
        segments = _split_entry_segments(block)
        for si, seg in enumerate(segments):
            text = _cell_text(seg)
            if not text or not _clean(text):
                continue
            known = _font_fields(seg)
            lines = [ln for ln in text.split("\n") if ln.strip()]
            if not lines:
                continue
            # 继承范围仅限「同一块内的续段」—— 每个 div.kbcontent 是独立课程，
            # 跨块继承会把「体育」顶成「大学英语」（真实踩过的坑）。
            fields = _classify_lines(lines, known, default_name=last_name if si > 0 else "")
            name = _clean(fields.get("name", ""))
            if not name or SEPARATOR_RE.fullmatch(name):
                continue
            last_name = name
            weeks_text = fields.get("weeks", "")
            weeks, guessed = parse_weeks(weeks_text, max_week)
            span = parse_periods_from_text(weeks_text, big_size)
            sp, ep = span if span else (start_period, end_period)
            out.append(
                Course(
                    name=name,
                    weekday=weekday,
                    start_period=sp,
                    end_period=ep,
                    weeks=weeks,
                    teacher=_clean(fields.get("teacher", "")),
                    location=_clean(fields.get("location", "")),
                    raw=_clean(text)[:300],
                    weeks_guessed=guessed,
                )
            )
    return out


# ---------------- 主入口 ----------------
def parse_timetable(
    html: str,
    *,
    max_week: int = 20,
    big_period_size: int = 2,
) -> list[Course]:
    """解析课表 HTML，返回去重后的课程列表。

    Raises:
        ParseError: 页面为空或未能识别出任何课程。
    """
    if not html or len(html) < 80:
        raise ParseError("课表页面内容为空，可能是会话失效或接口变更")

    soup = BeautifulSoup(html, "lxml")
    best: tuple[int, list[list[Tag | None]], dict, int, dict[int, int]] | None = None

    for table in soup.find_all("table"):
        grid, info = _expand_table(table)
        if not grid:
            continue
        header = _find_header(grid, info)
        if not header:
            continue
        row_idx, colmap = header
        if best is None or len(colmap) > best[0]:
            best = (len(colmap), grid, info, row_idx, colmap)

    if best is None:
        # 没有任何表头：按最常见的布局兜底（第 0 列节次，1~7 列周一~周日）
        for table in soup.find_all("table"):
            grid, info = _expand_table(table)
            if grid and len(grid[0]) >= 8 and len(grid) >= 3:
                best = (0, grid, info, 0, {i: i for i in range(1, 8)})
                break

    if best is None:
        raise ParseError("未在页面中找到课表表格（可能不是课表页，或教务系统已改版）")

    _, grid, info, header_row, colmap = best
    courses: list[Course] = []
    seen_cells: set[int] = set()

    for r in range(header_row + 1, len(grid)):
        row = grid[r]
        if not any(row):
            continue
        label = _cell_text(row[0])
        start_p, end_p = _parse_row_period(label, r - header_row - 1, big_period_size)

        for c, weekday in colmap.items():
            if c <= 0 or c >= len(row):
                continue
            cell = row[c]
            if cell is None:
                continue
            meta = info.get(id(cell), (r, c, 1, 1))
            if meta[0] != r or meta[1] != c:
                continue  # 合并单元格只在首格处理一次
            if id(cell) in seen_cells:
                continue
            seen_cells.add(id(cell))

            rowspan = meta[2]
            if rowspan > 1:
                last_label = _cell_text(grid[min(r + rowspan - 1, len(grid) - 1)][0])
                _, end_last = _parse_row_period(last_label, r + rowspan - 1 - header_row - 1, big_period_size)
                cell_end = max(end_p, end_last)
            else:
                cell_end = end_p

            try:
                courses.extend(_parse_cell(cell, weekday, start_p, cell_end, max_week, big_period_size))
            except Exception:
                # 单个单元格解析失败不应影响整张课表
                continue

    # 去重：同一门课可能同时出现在「理论课」「实验课」两张表里；
    # 以及简版/完整版双 div（kbcontent1 无教师字段 + kbcontent 有）——
    # 同槽位同周次时保留信息更全（有教师、周次非猜）的那份。
    uniq: dict[tuple, Course] = {}
    for c in courses:
        key = (c.name, c.weekday, c.start_period, c.end_period, tuple(c.weeks), c.location)
        old = uniq.get(key)
        if old is None:
            uniq[key] = c
            continue
        score = (bool(c.teacher), not c.weeks_guessed, len(c.location))
        old_score = (bool(old.teacher), not old.weeks_guessed, len(old.location))
        if score > old_score:
            uniq[key] = c

    result = sorted(uniq.values(), key=lambda x: (x.weekday, x.start_period, x.name))
    if not result:
        raise ParseError("课表表格已找到，但没有解析出任何课程（字段结构可能已变）")
    return result

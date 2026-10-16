"""Structure-aware RST -> clean text parser for the Odoo documentation.

Why our own parser instead of docutils/Sphinx:
* Odoo docs use Sphinx-only roles and directives (:guilabel:, :menuselection:, .. example::,
  .. tabs::, .. cards::) - plain docutils fails on them, full Sphinx needs the whole repo + build;
* we need the *structure* (section tree, blocks, labels) and not HTML;
* every rule below is explicit and testable (tests/test_rst_parser.py) - easy to defend.

Output of ``parse_rst``: document title + list of sections, each with a heading path
(["Wave transfers", "Create a wave", "Create a new wave"]) and a list of blocks
({"type": "paragraph|list|note|table|code|caption", "text": "..."}).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from common.utils import normalize_text

ADORNMENT_CHARS = set("=-~^*#+`\"':._")
ADMONITIONS = {
    "note": "Note",
    "tip": "Tip",
    "important": "Important",
    "warning": "Warning",
    "danger": "Danger",
    "caution": "Caution",
    "attention": "Attention",
    "hint": "Hint",
    "seealso": "See also",
    "example": "Example",
    "exercise": "Exercise",
    "spoiler": "",
    "admonition": "",
}
SKIP_DIRECTIVES = {"toctree", "raw", "include", "contents", "meta", "highlight", "rst-class", "tabularcolumns", "only", "index"}
CODE_DIRECTIVES = {"code-block", "code", "sourcecode", "math"}
CONTAINER_DIRECTIVES = {"tabs", "cards", "container", "group", "rubric"}
TITLED_DIRECTIVES = {"tab", "group-tab", "code-tab", "card"}

DIRECTIVE_RE = re.compile(r"^\.\.\s+([\w:-]+)::\s*(.*)$")
LABEL_RE = re.compile(r"^\.\.\s+_(.+):\s*$")
SUBST_DEF_RE = re.compile(r"^\.\.\s+\|([^|]+)\|\s+replace::\s*(.*)$")
FIELD_RE = re.compile(r"^:[\w-]+:(\s+[^`]*)?$")
LIST_RE = re.compile(r"^([-*+•]|#\.|\d+\.|\(?\d+\)|[a-zA-Z]\.)\s+")
ROLE_RE = re.compile(r":([a-zA-Z][\w-]*):`((?:[^`\\]|\\.)+?)`")
SIMPLE_TABLE_BORDER = re.compile(r"^=+( +=+)+\s*$")


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def is_adornment(line: str) -> bool:
    s = line.strip()
    return len(s) >= 3 and len(set(s)) == 1 and s[0] in ADORNMENT_CHARS and indent_of(line) == 0


def dedent(lines: list[str]) -> list[str]:
    non_blank = [indent_of(l) for l in lines if l.strip()]
    m = min(non_blank) if non_blank else 0
    return [l[m:] if l.strip() else "" for l in lines]


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "section"


def humanize_target(target: str) -> str:
    """'inventory/shipping_receiving/auto-waves' -> 'auto waves' (fallback for unknown refs)."""
    last = target.rstrip("/").split("/")[-1]
    last = last.split("#")[-1]
    return re.sub(r"[_\-]+", " ", last).strip()


@dataclass
class Heading:
    level: int
    title: str
    labels: list[str] = field(default_factory=list)


@dataclass
class Block:
    type: str
    text: str


@dataclass
class ParsedDoc:
    title: str
    sections: list[dict]
    labels: dict[str, str]  # label -> section title found in this document
    substitutions: dict[str, str]


class RstParser:
    def __init__(
        self,
        label_titles: Optional[dict[str, str]] = None,
        doc_titles: Optional[dict[str, str]] = None,
        doc_id: str = "",
        keep_image_alt: bool = False,
        keep_seealso: bool = True,
    ) -> None:
        self.label_titles = label_titles or {}
        self.doc_titles = doc_titles or {}
        self.doc_id = doc_id
        self.keep_image_alt = keep_image_alt
        self.keep_seealso = keep_seealso
        self.substitutions: dict[str, str] = {}
        self.heading_styles: list[tuple[str, str]] = []
        self.found_labels: dict[str, str] = {}

    # ================================================================ inline markup
    def resolve_doc(self, target: str) -> str:
        target = target.strip()
        base = self.doc_id.rsplit("/", 1)[0] if "/" in self.doc_id else ""
        if target.startswith("/"):
            # absolute: /applications/inventory_and_mrp/inventory/... -> strip to our id space
            candidate = re.sub(r"^/?(content/)?applications/inventory_and_mrp/", "", target)
        else:
            parts = (base.split("/") if base else []) + target.split("/")
            stack: list[str] = []
            for p in parts:
                if p == "..":
                    if stack:
                        stack.pop()
                elif p and p != ".":
                    stack.append(p)
            candidate = "/".join(stack)
        candidate = candidate.removesuffix(".rst")
        return self.doc_titles.get(candidate) or humanize_target(target)

    def _role(self, match: re.Match) -> str:
        role, content = match.group(1), match.group(2)
        content = content.replace("\\<", "<").replace("\\>", ">").replace("\\`", "`")
        explicit = re.match(r"^(.*?)\s*<([^<>]+)>$", content, flags=re.S)
        if role == "icon":
            return ""
        if role == "menuselection":
            parts = [p.strip() for p in content.split("-->")]
            return " > ".join(p for p in parts if p)
        if role in ("ref", "doc", "any", "numref", "term", "option"):
            if explicit and explicit.group(1).strip():
                return explicit.group(1).strip()
            target = explicit.group(2) if explicit else content
            if role == "doc":
                return self.resolve_doc(target)
            return self.label_titles.get(target.strip().lower()) or humanize_target(target)
        if role == "abbr":
            return content.strip()
        if role in ("math",):
            return content
        # guilabel, dfn, sup, sub, kbd, command, file, samp, program, ...
        return explicit.group(1) if (explicit and role not in ("guilabel", "kbd")) else content

    def inline(self, text: str, _depth: int = 0) -> str:
        if _depth < 3 and self.substitutions:
            text = re.sub(
                r"\|([^|\s][^|]*?)\|(_{0,2})",
                lambda m: self.inline(self.substitutions[m.group(1)], _depth + 1)
                if m.group(1) in self.substitutions
                else m.group(0),
                text,
            )
        text = ROLE_RE.sub(self._role, text)
        text = re.sub(r"`([^`<]+?)\s*<[^>]+>`_{1,2}", r"\1", text)  # `text <url>`_
        text = re.sub(r"`([^`]+)`_{1,2}", r"\1", text)  # `ref`_
        text = re.sub(r"``(.+?)``", r"\1", text)  # ``literal``
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)  # **strong**
        text = re.sub(r"(?<![\w*\\])\*(?![\s*])(.+?)(?<![\s\\])\*(?![\w*])", r"\1", text)  # *emphasis*
        text = re.sub(r"(?<!\w)`([^`]+)`(?!\w)", r"\1", text)  # `default role`
        text = text.replace("\\ ", "")
        text = re.sub(r"\\(.)", r"\1", text)
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"\s+([,.;:!?)])", r"\1", text)
        text = re.sub(r"\(\s+", "(", text)
        return text.strip()

    # ================================================================ block parser
    def parse(self, text: str) -> ParsedDoc:
        text = text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4)
        lines = text.split("\n")
        # pass 1: substitution definitions (may appear anywhere in the file)
        for line in lines:
            m = SUBST_DEF_RE.match(line)
            if m:
                self.substitutions[m.group(1)] = m.group(2).strip()
        items = self.parse_lines(lines, top=True)
        return self._build_sections(items)

    def _heading_level(self, style: tuple[str, str]) -> int:
        if style not in self.heading_styles:
            self.heading_styles.append(style)
        return self.heading_styles.index(style) + 1

    def parse_lines(self, lines: list[str], top: bool = False) -> list:
        items: list = []
        pending_labels: list[str] = []
        n = len(lines)
        i = 0
        while i < n:
            line = lines[i]
            stripped = line.strip()
            if not stripped:
                i += 1
                continue
            ind = indent_of(line)

            # ---- indented block (block quote) -> parse recursively
            if ind > 0:
                j = i
                while j < n and (not lines[j].strip() or indent_of(lines[j]) >= ind):
                    j += 1
                items.extend(self.parse_lines(dedent(lines[i:j])))
                i = j
                continue

            # ---- section titles (only at top level)
            if top and is_adornment(line) and i + 2 < n and lines[i + 1].strip() and is_adornment(lines[i + 2]) \
                    and lines[i + 2].strip()[0] == stripped[0]:
                level = self._heading_level(("over", stripped[0]))
                items.append(Heading(level, self.inline(lines[i + 1].strip()), pending_labels))
                pending_labels = []
                i += 3
                continue
            if top and i + 1 < n and is_adornment(lines[i + 1]) and not is_adornment(line) \
                    and not line.startswith(".. ") and len(lines[i + 1].strip()) >= min(len(stripped), 3):
                level = self._heading_level(("under", lines[i + 1].strip()[0]))
                items.append(Heading(level, self.inline(stripped), pending_labels))
                pending_labels = []
                i += 2
                continue

            # ---- explicit markup: directives, labels, substitutions, comments
            if stripped.startswith(".."):
                j = i + 1
                while j < n and (not lines[j].strip() or indent_of(lines[j]) > 0):
                    j += 1
                body = lines[i + 1:j]
                label = LABEL_RE.match(stripped)
                directive = DIRECTIVE_RE.match(stripped)
                if label:
                    pending_labels.append(label.group(1).strip().lower())
                elif SUBST_DEF_RE.match(stripped):
                    pass
                elif directive:
                    items.extend(self._directive(directive.group(1).lower(), directive.group(2), body))
                # else: comment -> skipped
                i = j
                continue

            # ---- field list (":nosearch:", ":show-content:") -> metadata, skipped
            if FIELD_RE.match(stripped):
                i += 1
                continue

            # ---- grid table
            if stripped.startswith("+-") or stripped.startswith("+="):
                j = i
                while j < n and lines[j].strip():
                    j += 1
                table = self._grid_table(lines[i:j])
                if table:
                    items.append(Block("table", table))
                i = j
                continue

            # ---- simple table
            if SIMPLE_TABLE_BORDER.match(line):
                j, table = self._simple_table(lines, i)
                if table:
                    items.append(Block("table", table))
                i = j
                continue

            # ---- lists
            if LIST_RE.match(line):
                j, block = self._list(lines, i)
                if block:
                    items.append(block)
                i = j
                continue

            # ---- paragraph (until blank line)
            j = i
            while j < n and lines[j].strip() and indent_of(lines[j]) == 0:
                j += 1
            para = " ".join(l.strip() for l in lines[i:j])
            literal_follows = para.endswith("::")
            if literal_follows:
                para = para[:-2].rstrip()
                para = para + ":" if para and not para.endswith(":") else para
            text = self.inline(para)
            if text:
                items.append(Block("paragraph", text))
            i = j
            if literal_follows:
                k = i
                while k < n and not lines[k].strip():
                    k += 1
                if k < n and indent_of(lines[k]) > 0:
                    m = k
                    while m < n and (not lines[m].strip() or indent_of(lines[m]) > 0):
                        m += 1
                    code = "\n".join(dedent(lines[k:m])).strip()
                    if code:
                        items.append(Block("code", code))
                    i = m
        if pending_labels and items:
            # labels at the end of a block (rare) point to the current section
            items.append(Heading(-1, "", pending_labels))
        return items

    # ---------------------------------------------------------------- directives
    def _split_options(self, body: list[str]) -> tuple[dict[str, str], list[str]]:
        body = dedent(body)
        options: dict[str, str] = {}
        k = 0
        # an option is ":name:" followed by a space or end of line; ":ref:`...`" is a role, not an option
        while k < len(body) and re.match(r"^:[\w-]+:(\s|$)", body[k].strip()):
            m = re.match(r"^:([\w-]+):\s*(.*)$", body[k].strip())
            if m:
                options[m.group(1)] = m.group(2)
            k += 1
        return options, body[k:]

    def _render_items(self, items: list) -> list[str]:
        out = []
        for it in items:
            if isinstance(it, Block) and it.text:
                out.append(it.text)
        return out

    def _directive(self, name: str, arg: str, body: list[str]) -> list:
        options, content = self._split_options(body)
        if name in SKIP_DIRECTIVES:
            return []
        if name == "image":
            alt = options.get("alt", "")
            return [Block("caption", f"[Image: {self.inline(alt)}]")] if (self.keep_image_alt and alt) else []
        if name == "figure":
            caption = self._render_items(self.parse_lines(content))
            return [Block("caption", " ".join(caption))] if caption else []
        if name in CODE_DIRECTIVES:
            code = "\n".join(content).strip() or arg.strip()
            return [Block("code", code)] if code else []
        if name == "list-table":
            table = self._list_table(content, int(options.get("header-rows", "0") or 0))
            title = self.inline(arg) if arg else ""
            if not table:
                return []
            return [Block("table", (title + "\n" if title else "") + table)]
        if name in ADMONITIONS:
            if name == "seealso" and not self.keep_seealso:
                return []
            label = ADMONITIONS[name]
            if name in ("admonition", "spoiler") or (arg and name in ("example", "exercise")):
                label = self.inline(arg) or label
                lines = content
            else:
                # the argument is the first line of the content and may continue on the next
                # lines (".. seealso:: :doc:`text\n   <target>`"), so they are parsed together
                lines = ([arg] + content) if arg.strip() else content
            parts = self._render_items(self.parse_lines(lines))
            if not parts:
                return []
            text = "\n".join(parts)
            return [Block("note", f"{label}: {text}" if label else text)]
        if name in TITLED_DIRECTIVES:
            title = self.inline(arg)
            parts = self._render_items(self.parse_lines(content))
            if not parts:
                return [Block("paragraph", title)] if title else []
            blocks = [Block("paragraph", f"{title}: {parts[0]}" if title else parts[0])]
            blocks += [Block("paragraph", p) for p in parts[1:]]
            return blocks
        if name in CONTAINER_DIRECTIVES:
            return [it for it in self.parse_lines(content) if isinstance(it, Block)]
        # unknown directive: keep its text, never lose content silently
        return [it for it in self.parse_lines(content) if isinstance(it, Block)]

    # ---------------------------------------------------------------- lists
    def _list(self, lines: list[str], i: int) -> tuple[int, Optional[Block]]:
        n = len(lines)
        entries: list[str] = []
        ordered = False
        number = 0
        while i < n:
            line = lines[i]
            m = LIST_RE.match(line) if line.strip() and indent_of(line) == 0 else None
            if not m:
                break
            marker = m.group(1)
            is_ordered = not marker in ("-", "*", "+", "•")
            ordered = ordered or is_ordered
            first = line[m.end():]
            j = i + 1
            while j < n and (not lines[j].strip() or indent_of(lines[j]) > 0):
                j += 1
            rest = lines[i + 1:j]
            while rest and not rest[-1].strip():
                rest.pop()
            sub_items = self.parse_lines([first] + dedent(rest)) if (first.strip() or rest) else []
            texts: list[str] = []
            for it in sub_items:
                if isinstance(it, Block) and it.text:
                    if it.type == "list":
                        texts.append("\n".join("  " + l for l in it.text.split("\n")))
                    else:
                        texts.append(it.text)
            number += 1
            prefix = f"{number}." if is_ordered else "-"
            if texts:
                head, *tail = texts
                entry = f"{prefix} {head}"
                for t in tail:
                    entry += ("\n" + t) if t.startswith("  ") else ("\n  " + t)
                entries.append(entry)
            i = j
            # skip blank lines between items, stop if next non-blank is not an item
            k = i
            while k < n and not lines[k].strip():
                k += 1
            if k < n and LIST_RE.match(lines[k]) and indent_of(lines[k]) == 0:
                i = k
            else:
                break
        if not entries:
            return i, None
        return i, Block("list", "\n".join(entries))

    # ---------------------------------------------------------------- tables
    def _format_rows(self, rows: list[list[str]], header_rows: int) -> str:
        rows = [[self.inline(c) for c in r] for r in rows if any(c.strip() for c in r)]
        if not rows:
            return ""
        if header_rows and len(rows) > header_rows:
            header = rows[0]
            out = []
            for r in rows[header_rows:]:
                cells = []
                for h, c in zip(header, r):
                    if c:
                        cells.append(f"{h}: {c}" if h else c)
                out.append("; ".join(cells))
            return "\n".join(out)
        return "\n".join(" | ".join(c for c in r if c) for r in rows)

    def _list_table(self, content: list[str], header_rows: int) -> str:
        rows: list[list[str]] = []
        for line in content:
            if not line.strip():
                continue
            bare = line.strip().lstrip("*- ").strip()
            if bare.startswith((".. image::", ".. figure::", ":alt:", ":align:", ":width:", ":scale:")):
                # screenshots inside table cells: keep the cell, drop the image markup
                line = line[: len(line) - len(line.lstrip())] + line.strip()[: len(line.strip()) - len(bare)]
                if not line.strip() or line.strip() in ("*", "-", "* -"):
                    if line.strip() in ("-", "* -"):
                        line = line.rstrip() + " "
                    else:
                        continue
            m_row = re.match(r"^\*\s+-\s*(.*)$", line)
            m_row_alone = re.match(r"^\*\s*$", line)
            m_cell = re.match(r"^\s+-\s*(.*)$", line)
            if m_row:
                rows.append([m_row.group(1)])
            elif m_row_alone:
                rows.append([])
            elif m_cell and rows:
                rows[-1].append(m_cell.group(1))
            elif rows and rows[-1]:
                rows[-1][-1] += " " + line.strip()
        return self._format_rows(rows, header_rows)

    def _grid_table(self, lines: list[str]) -> str:
        border = lines[0].rstrip()
        cols = [k for k, ch in enumerate(border) if ch == "+"]
        if len(cols) < 2:
            return ""
        rows: list[list[str]] = []
        current: list[list[str]] = [[] for _ in range(len(cols) - 1)]
        header_rows = 0
        for line in lines[1:]:
            s = line.rstrip()
            if s.startswith("+"):
                if any(current_cell for current_cell in current):
                    rows.append([" ".join(c).strip() for c in current])
                current = [[] for _ in range(len(cols) - 1)]
                if "=" in s:
                    header_rows = len(rows)
                continue
            for c in range(len(cols) - 1):
                cell = s[cols[c] + 1: cols[c + 1]] if len(s) > cols[c] + 1 else ""
                cell = cell.strip().strip("|").strip()
                if cell:
                    current[c].append(cell)
        return self._format_rows(rows, header_rows)

    def _simple_table(self, lines: list[str], i: int) -> tuple[int, str]:
        n = len(lines)
        border = lines[i]
        spans = [(m.start(), m.end()) for m in re.finditer(r"=+", border)]
        rows: list[list[str]] = []
        borders = 1
        header_rows = 0
        j = i + 1
        while j < n:
            line = lines[j]
            if SIMPLE_TABLE_BORDER.match(line):
                borders += 1
                if borders == 2:
                    header_rows = len(rows)
                if borders >= 3 or (j + 1 < n and not lines[j + 1].strip() and borders >= 2 and header_rows == 0):
                    j += 1
                    break
                j += 1
                continue
            if not line.strip():
                j += 1
                continue
            row = []
            for k, (a, b) in enumerate(spans):
                end = spans[k + 1][0] if k + 1 < len(spans) else len(line)
                row.append(line[a:end].strip())
            rows.append(row)
            j += 1
        if borders < 3:
            header_rows = 0
        return j, self._format_rows(rows, header_rows)

    # ---------------------------------------------------------------- sections
    def _build_sections(self, items: list) -> ParsedDoc:
        title = ""
        sections: list[dict] = []
        stack: list[tuple[int, str]] = []
        current: Optional[dict] = None

        def new_section(path: list[str], level: int) -> dict:
            sec = {
                "heading_path": path,
                "title": path[-1] if path else "",
                "level": level,
                "anchor": slugify(path[-1]) if len(path) > 1 else "",
                "blocks": [],
            }
            sections.append(sec)
            return sec

        for it in items:
            if isinstance(it, Heading):
                if it.level == -1:  # trailing labels
                    for lab in it.labels:
                        self.found_labels[lab] = current["title"] if current else title
                    continue
                if not title:
                    title = it.title
                    stack = [(it.level, it.title)]
                    current = new_section([it.title], it.level)
                else:
                    while stack and stack[-1][0] >= it.level:
                        stack.pop()
                    if not stack:  # a second top-level title: treat as level-2 under the doc title
                        stack = [(0, title)]
                    stack.append((it.level, it.title))
                    current = new_section([t for _, t in stack], it.level)
                for lab in it.labels:
                    self.found_labels[lab] = it.title
            else:
                if current is None:
                    current = new_section([], 0)
                text = normalize_text(it.text)
                if text:
                    current["blocks"].append({"type": it.type, "text": text})
        if sections and not sections[0]["heading_path"]:
            sections[0]["heading_path"] = [title] if title else []
            sections[0]["title"] = title
        sections = [s for s in sections if s["blocks"]]
        return ParsedDoc(title=title, sections=sections, labels=self.found_labels, substitutions=self.substitutions)


def parse_rst(text: str, **kwargs) -> ParsedDoc:
    return RstParser(**kwargs).parse(text)

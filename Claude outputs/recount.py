import re
HUNK = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$')

def _is_file_header(lines, i):
    """--- X, +++ Y, then @@ : a real file header (not a removed line that starts with '-- ')."""
    return (lines[i].startswith('--- ') and i + 2 < len(lines)
            and lines[i + 1].startswith('+++ ') and HUNK.match(lines[i + 2]) is not None)

def recount_patch(text: str) -> str:
    """Rewrite every hunk header so its line counts match the hunk body.
    Git's own --recount mis-reads the next file's '--- ' header as a removed line."""
    lines = text.split('\n')
    out, i = [], 0
    while i < len(lines):
        m = HUNK.match(lines[i])
        if not m:
            out.append(lines[i]); i += 1; continue
        j, old, new = i + 1, 0, 0
        while j < len(lines):
            l = lines[j]
            if HUNK.match(l) or l.startswith('diff ') or _is_file_header(lines, j):
                break
            if l.startswith('\\'):
                pass
            elif l.startswith('+'): new += 1
            elif l.startswith('-'): old += 1
            elif l.startswith(' ') or l == '':
                if l == '' and j == len(lines) - 1:   # trailing newline artifact
                    break
                old += 1; new += 1
            else:
                break
            j += 1
        o_start, n_start = m.group(1), m.group(3)
        out.append(f"@@ -{o_start},{old} +{n_start},{new} @@{m.group(5)}")
        out.extend(lines[i + 1:j])
        i = j
    return '\n'.join(out)

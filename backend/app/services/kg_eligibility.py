"""Shared eligibility rules for papers shown in the research knowledge graph.

Only papers explicitly classified as normal library material participate. Test and
excluded records remain stored for diagnostics and can be reclassified without
destructive cleanup.
"""

from ..db import row

NORMAL_ROLE = "normal"
NON_GRAPH_ROLES = {"test", "excluded"}
VALID_ROLES = {NORMAL_ROLE, *NON_GRAPH_ROLES}


def paper_is_eligible(paper_id: str) -> bool:
    return bool(row(
        "SELECT 1 FROM papers WHERE id=? AND COALESCE(library_role, 'normal')='normal'",
        (paper_id,),
    ))


def set_paper_role(paper_id: str, role: str) -> bool:
    if role not in VALID_ROLES:
        raise ValueError(f"library_role 必须是以下值之一：{', '.join(sorted(VALID_ROLES))}")
    if not row("SELECT id FROM papers WHERE id=?", (paper_id,)):
        return False
    from ..db import ex
    ex("UPDATE papers SET library_role=? WHERE id=?", (role, paper_id))
    return True

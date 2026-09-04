"""추출 프롬프트. enable_memory=False가 기본이지만 MemoryRuntime.build가 문자열을 요구하므로 둔다."""
from __future__ import annotations

from commerce_common.memory import MEMORY_EXTRACTION_TEMPLATE

ATWORKS_MEMORY_EXTRACTION_PROMPT = MEMORY_EXTRACTION_TEMPLATE.format(
    keeper="an assistant",
    subject="one aTworks project",
    occasions="sessions with its operator",
    speaker="the operator",
    qualifies=(
        "a field-name alias the operator stated (e.g. 계약번호 means contractNo), a scorer they "
        "prefer, how they like the digest laid out."
    ),
    standalone_example='"계약번호 = contractNo" tells a future reader everything',
    live_key_rule='Keep one live goal under the key "current_goal".',
    excluded=(
        "anything from run results, response bodies, or uploaded files; server names, target "
        "environments, credentials; anything about an identifiable person."
    ),
)

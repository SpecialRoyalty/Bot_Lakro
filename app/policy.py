from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ModerationAction(str, Enum):
    ALLOW = "allow"
    DELETE = "delete"
    BAN = "ban"
    SANCTION = "sanction"


@dataclass(frozen=True, slots=True)
class MessagePolicyContext:
    membership_event: bool
    privileged: bool
    group_open: bool
    story: bool
    forwarded: bool
    forwards_forbidden: bool
    contains_link: bool
    links_forbidden: bool
    allowed_content: bool
    contains_banned_word: bool


def decide_message_action(context: MessagePolicyContext) -> ModerationAction:
    """Return one deterministic action using the documented priority order."""
    # Telegram's entry/exit service notices are housekeeping messages, not
    # administrator content. They are always removed, even when an admin added
    # or removed the member.
    if context.membership_event:
        return ModerationAction.DELETE

    # Environment admins and Telegram group admins are exempt from all content
    # moderation rules.
    if context.privileged:
        return ModerationAction.ALLOW

    # Closing is the strongest content rule: nothing sent by a regular member
    # survives outside the active window or while automatic opening is OFF.
    if not context.group_open:
        return ModerationAction.DELETE

    # A shared Telegram story is explicitly punishable, even though Telegram
    # technically represents it as forwarded content.
    if context.story:
        return ModerationAction.BAN

    # The forwarding rule explicitly says "delete without punishment", so it
    # takes priority over links or forbidden words inside that forward.
    if context.forwards_forbidden and context.forwarded:
        return ModerationAction.DELETE

    # Links are bannable even when attached to a media type that would otherwise
    # only be deleted.
    if context.links_forbidden and context.contains_link:
        return ModerationAction.BAN

    if not context.allowed_content:
        return ModerationAction.DELETE

    if context.contains_banned_word:
        return ModerationAction.SANCTION

    return ModerationAction.ALLOW


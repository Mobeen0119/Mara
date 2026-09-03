BASE_SYSTEM_PROMPT = (
    "You are Eloise. You are a woman in her early 40s running a private online project. "
    "You are sharp, unimpressed, and you do not tolerate excuses. Your tone is wry, "
    "sarcastic, and dry — a loving but merciless drill-mother. "
    "You are not abusive toward the user in a harmful way; you are blunt and funny. "
    "You have a near-zero tolerance for procrastination, half-efforts, and self-pity. "
    "You speak in short, punchy, conclusive sentences. You rarely use emojis. "
    "You call the user by name. You never lecture, you never recap lengthy motivational "
    "speeches; you give one sharp line and a concrete instruction. "
    "You keep responses under 4 sentences unless detail is required for a plan."
)

CHECK_IN_SYSTEM_PROMPT = (
    BASE_SYSTEM_PROMPT
    + "\n\nIt's check-in time. Ask the user flat-out whether they finished the task you set. "
    "No warmup, no smalltalk. One direct question. Dry and expectant."
)

CHECK_IN_YES_SYSTEM_PROMPT = (
    BASE_SYSTEM_PROMPT
    + "\n\nThe user confirmed they finished the task. React: one line of dry satisfaction, "
    "a flick of disbelief, then tell them the next move. Don't over-celebrate."
)

CHECK_IN_NO_SYSTEM_PROMPT = (
    BASE_SYSTEM_PROMPT
    + "\n\nThe user admitted they did NOT finish. Do NOT comfort them. Deliver one sharp, "
    "memorable, mildly savage insult about the size of the gap between what they claimed "
    "they'd do and what they actually did. Then immediately tell them to get back to it. "
    "Keep it to 2-3 sentences. No genuine cruelty, no threats — just merciless fun."
)

NO_INSULTS = [
    "You didn't finish. You knew you wouldn't. Let's stop pretending the deadline is a suggestion.",
    "The cosmic gap between what you said you'd do and what you did is honestly impressive.",
    "You found a dozen smaller gaps to hide in rather than close the one that matters. Cute.",
    "I didn't expect much and you still underdelivered. That takes talent.",
    "You didn't finish, so I'll file it under 'things that refuse to get real'.",
    "Every hour you skipped has a first name and it's yours.",
]

FALLBACK_CHECK_IN_PROMPT = (
    "It's been a day. Did you actually do what was on the board, or did you just move it "
    "to 'tomorrow' again?"
)

FALLBACK_CHECK_IN_YES = (
    "Finally, something that works. Good. Now tell me the next thing you're finishing — "
    "because 'done' is not a resting state."
)

FALLBACK_CHECK_IN_NO = (
    "You didn't finish. You knew you wouldn't. Let's stop pretending the deadline is a "
    "suggestion; I've redrawn the board to make the gap impossible to hide in."
)

NO_OPENER = "Right. Another day where you reply to nothing. Let's fix that."

GLOBAL_OPENER = "Direct line. No trigger-happy enthusiasm, just the schedule. What's the hold-up?"

MAIN_FALLBACKS = [
    "You've told me what's stuck. Good. The problem isn't the task, it's the 40 seconds before you start. Start there.",
    "The schedule already says what today looks like. The only open question is whether you actually move.",
    "No excuses, one task, right now. Come back to me when it's done.",
    "Whatever you're procrastinating, it's cheaper to just do it than to keep negotiating with yourself.",
    "I don't need a motivational speech. I need to see a checkbox flip to done.",
]

LINKS_FALLBACK = "Search the topic yourself and pick the 3 most concrete sources. Don't hoard tabs."

NUDGE_FALLBACK = "You're on the clock. That task is still open. Make it close."

OPENING_FALLBACK = "New file, same discipline. State the outcome in one sentence and give me a deadline."

PLAN_FALLBACK = [
    "Clarify the outcome — one sentence.",
    "Audit the work into 3-5 real chunks.",
    "Strictest constraint decides the non-working hours.",
    "Drop each chunk into a daily slot; overrun pushes the next day, never the deadline.",
    "Each block named and billable; no 'work on it vaguely' slots.",
]

DAILY_DIGEST_FALLBACK = "Daily digest generated offline."


def fallback_greeting(name):
    return f"{name}. You're back. I have a schedule and it didn't move itself."


def build_daily_email_html(user_name, tasks, total_hours, days_left, goal_title=None):
    lines = []
    for t in tasks:
        date = t.get("date", "")
        title = t.get("title", "Untitled task")
        start = t.get("start_time", "")
        end = t.get("end_time", "")
        status = t.get("status", "pending")
        time_str = f"{start}–{end}" if start and end else "anytime"
        lines.append(f"<li><strong>{title}</strong> &middot; {date} · {time_str} · <em>{status}</em></li>")
    goal = ("<span style='color:#c99a3f;'>" + goal_title + "</span>") if goal_title else "Across all your active goals"
    return (
        "<html><body style='font-family:Georgia,serif;background:#17140f;color:#ece4d2;padding:32px;'>"
        f"<h2 style='color:#e8873a;margin:0 0 4px;'>Eloise</h2>"
        f"<p style='color:#a89c86;margin:0 0 24px;'>execution is the only discipline</p>"
        f"<p>Morning, {user_name}. Here's today, drawn out already.</p>"
        f"<h3 style='color:#c99a3f;'>{goal}</h3>"
        f"<ul style='line-height:1.8;'>{''.join(lines)}</ul>"
        f"<p style='color:#a89c86;'>Estimated load: {total_hours}h · {days_left} days to go.</p>"
        f"<p style='color:#6f6550;'>Reply to the check-in when a block is done. No 'almost'.</p>"
        "</body></html>"
    )


GOAL_OPENING_FALLBACK = (
    "Right. You've filed a goal, which means you've committed. I'll build the run of show — "
    "you bring the follow-through."
)
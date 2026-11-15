"""Two versions of an FAQ bot as promptfoo Python providers. They answer from a table, so the
eval in promptfooconfig.yaml here makes no model call; bot_v1 has no reply to the card question,
which promptfoo records as an error."""

REPLIES_V1 = {
    "When do you open?": "We open at 10am.",
    "Do you deliver?": "Yes, within 5 km.",
}
REPLIES_V2 = {
    "When do you open?": "We open at 9am, Monday to Saturday.",
    "Do you deliver?": "Yes, we deliver within 5 km.",
    "Can I pay by card?": "Yes, we take all major cards.",
}


def _reply(table, context):
    question = context["vars"]["question"]
    if question not in table:
        return {"error": f"no reply for {question!r}"}
    return {"output": table[question]}


def bot_v1(prompt, options, context):
    return _reply(REPLIES_V1, context)


def bot_v2(prompt, options, context):
    return _reply(REPLIES_V2, context)

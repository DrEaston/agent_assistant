"""Hands-free cook mode: decide whether a phrase heard by the always-on mic was meant for Dieter,
and whether it's a quick command the tablet can carry out by itself (no AI needed).

Rules that keep kitchen chatter from triggering things:
- "Dieter" (or a common mishearing of it) only counts as the first word or two of a phrase.
- Right after Dieter speaks there's a short follow-up window where the name isn't needed (the tablet tracks it).
- Quick commands must be the whole phrase ("next step"), not words inside a longer sentence.
- Everything else goes to the AI, which may still decide the phrase wasn't meant for it.
"""

import re
from difflib import SequenceMatcher

# What speech recognition tends to hear when someone says "Dieter".
WAKE_WORDS = {
    "dieter", "deiter", "dieder", "dietor", "deeter", "deter", "dieta", "dietr", "diter", "dita", "deda", "dida",
    "peter", "pieter", "dieter's", "detour", "deader", "teeter", "dealer", "dieters", "ditor",
}
WAKE_PAIRS = {("the", "tour"), ("dee", "ter"), ("die", "ter"), ("d", "ter"), ("the", "tor")}
FILLERS = {"hey", "hi", "ok", "okay", "yo", "um", "uh", "so", "and", "oh", "alright", "excuse", "me", "please"}
MAX_COMMAND_WORDS = 8

_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "ninety": 90,
}


def normalize(text):
    text = str(text or "").lower().replace("’", "'").replace("-", " ")
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _is_wake(word):
    return word in WAKE_WORDS or SequenceMatcher(None, word, "dieter").ratio() >= 0.8


def hear(text, follow_up=False):
    """-> {"addressed": bool, "rest": what they said after the name, "via": "name"|"follow-up"|"", "near_miss": bool}"""
    words = normalize(text).split()
    i = 0
    while i < len(words) and i < 2 and words[i] in FILLERS:
        i += 1
    if i < len(words) and _is_wake(words[i]):
        return {"addressed": True, "rest": " ".join(words[i + 1:]), "via": "name", "near_miss": False}
    if i + 1 < len(words) and (words[i], words[i + 1]) in WAKE_PAIRS:
        return {"addressed": True, "rest": " ".join(words[i + 2:]), "via": "name", "near_miss": False}
    if follow_up and words:
        return {"addressed": True, "rest": " ".join(words), "via": "follow-up", "near_miss": False}
    # A first word that's close-ish to "Dieter" is logged so the list above can be tuned.
    near = bool(words) and SequenceMatcher(None, words[min(i, len(words) - 1)], "dieter").ratio() >= 0.7
    return {"addressed": False, "rest": "", "via": "", "near_miss": near}


def _minutes(phrase):
    """'12' / 'twelve' / 'twenty five' / 'an hour' / 'half an hour' / 'one and a half hours' -> minutes, or None."""
    words = phrase.split()
    if not words:
        return None
    if " ".join(words) in ("half an hour", "half hour"):
        return 30
    unit = 1
    if words[-1] in ("hour", "hours"):
        unit = 60
        words = words[:-1]
    elif words[-1] in ("minute", "minutes", "min", "mins"):
        words = words[:-1]
    half = 0.0
    if words[-2:] == ["a", "half"] and len(words) > 2 and words[-3] == "and":
        half, words = 0.5, words[:-3]
    total = 0
    for word in words:
        if word.isdigit():
            total += int(word)
        elif word in _NUMBERS:
            total += _NUMBERS[word]
        else:
            return None
    if not total:
        return None
    minutes = round((total + half) * unit)
    return minutes if 1 <= minutes <= 600 else None


_VERB = re.compile(r"^(?:(?:can you|could you|please) )?(?:start|set|make|give me|put on|add|do)(?: me)? ")
_UNIT = r"(?P<u>minutes?|mins?|hours?)(?P<half> and a half)?"
_TIMER = [
    # "a 12 minute timer (for the pie)", "an hour timer", "half an hour timer"
    re.compile(r"^(?:(?P<n>.+?) )?" + _UNIT + r" timer(?: for (?:the )?(?P<label>.+))?$"),
    # "a timer for 12 minutes (for the pie)", "timer 12 minutes"
    re.compile(r"^(?:an? )?timer (?:for )?(?P<n>.+?) " + _UNIT + r"(?: for (?:the )?(?P<label>.+))?$"),
]


def _timer(phrase):
    body = _VERB.sub("", phrase)
    for pattern in _TIMER:
        match = pattern.match(body)
        if not match:
            continue
        amount = (match.group("n") or "").split()
        if len(amount) > 1 and amount[0] in ("a", "an"):
            amount = amount[1:]  # "a 12 minute timer": the article isn't a number
        half = ["and", "a", "half"] if match.group("half") else []
        minutes = _minutes(" ".join(amount + half + [match.group("u")]))
        if minutes:
            return {"type": "timer", "minutes": minutes, "label": (match.group("label") or "").strip()}
    return None


_PATTERNS = [
    ("next", r"(?:what's |what is |whats )?(?:the )?next(?: step)?|what now|what do i do(?: now| next)?|what's next|next one"),
    ("read", r"(?:read|repeat|say)(?: it| that| the step| this step| the last one)?(?: again)?|come again|what was that|say again"),
    ("ingredients", r"(?:read )?(?:me )?(?:the )?ingredients|what are the ingredients|what goes in(?: it)?|what do i need"),
    ("timer_left", r"how (?:long|much time)(?: is| s)?(?: left| remaining)?(?: on (?:the|my) timers?)?|how's the timer|"
                   r"timer status|how long left|time left|how much longer"),
    ("stop_timer", r"(?:stop|cancel|kill|turn off|end)(?: the| that| my)? (?:timer|alarm)s?"),
    ("done", r"(?:mark (?:it|that|this)(?: as)? )?done|(?:i'm|im|i am) done(?: with (?:it|that|this))?|finished|"
             r"check (?:it|that) off|that's done|thats done|it's done|its done"),
    ("quiet", r"quiet|stop|shush|hush|stop talking|be quiet|enough|okay stop|that's enough"),
    ("yes", r"yes|yeah|yep|yup|sure|do it|apply(?: it)?|go ahead|sounds good|ok do it|okay do it|yes please|perfect"),
    ("no", r"no|nope|cancel|never ?mind|don't|do not|forget it|no thanks|no thank you"),
    ("undo", r"undo(?: that| it)?|take that back|put it back"),
    ("sleep", r"stop listening|go to sleep|hands free off|turn off hands free|that's all|goodnight|good night"),
    ("close", r"close(?: it| this| that)?(?: the)?(?: chat| window| panel)?|bye|bye bye|goodbye|bye dieter|see you|"
              r"we're done|were done|all done|done talking|that's it|thats it|exit|go away|"
              r"thanks that's all|thank you that's all|thanks that's it|thank you that's it"),
    ("handsfree", r"(?:let's |lets )?(?:go |turn on |start )?hands free(?: on| mode)?"),
]
_COMPILED = [(name, re.compile(rf"^(?:{pattern})(?: please)?$")) for name, pattern in _PATTERNS]
_AMOUNT = re.compile(r"^how (?:much|many) (?P<item>.+?)(?: do i (?:need|use|add|put in))?(?: in (?:it|this|there))?$")
_GOTO = re.compile(r"^(?:go to|switch to|open|show(?: me)?|let's do|lets do|on to|over to)(?: the)? (?P<dish>.+)$")


def command(rest):
    """A quick command the tablet can do itself, or None (then the AI handles it)."""
    phrase = normalize(rest)
    if not phrase:
        return {"type": "wake"}
    if "timer" in phrase.split():
        timer = _timer(phrase)  # strict pattern, so longer phrases are fine ("...for an hour and a half for the turkey")
        if timer:
            return timer
    if len(phrase.split()) > MAX_COMMAND_WORDS:
        return None
    for name, pattern in _COMPILED:
        if pattern.match(phrase):
            return {"type": name}
    match = _AMOUNT.match(phrase)
    if match:
        return {"type": "amount", "item": match.group("item")}
    match = _GOTO.match(phrase)
    if match:
        return {"type": "goto", "dish": match.group("dish")}
    return None


_SPOKEN = ("This phrase came from the hands-free microphone in a busy kitchen and your reply is read aloud: keep it "
           "to 1-2 short spoken sentences, no lists, no symbols. Describe a proposed change in plain words; the cook "
           "will say yes or no. ")
_BY_NAME = ("It started with what sounded like \"Dieter\", so it is almost certainly for you. Only if the speaker was "
            "plainly talking to someone else (speech recognition sometimes hears \"Peter\" as Dieter), reply with "
            "{\"ignore\": true} and nothing else.")
_FOLLOW_UP = ("It was heard in the few seconds after you last spoke, without your name, so it may be a reply to you or "
              "just people talking to each other or the TV. If it isn't clearly meant for you, reply with "
              "{\"ignore\": true} and nothing else.")


_CONVERSATION = ("The cook tapped the mic to talk with you for a couple of minutes; every phrase the mic hears comes to "
                 "you, without your name. Treat it as a conversation: short follow-ups (\"and the gravy?\", \"Sunday "
                 "instead\") are for you. If the phrase is clearly not for you (people talking to each other, the TV, "
                 "a different topic than cooking and this plan), reply with {\"ignore\": true} and nothing else. If "
                 "the cook is wrapping up (\"thanks\", \"that's it\", \"got it\"), give a short sign-off and add "
                 "\"end\": true.")


def spoken_prompt(via):
    """Extra instructions for the AI when a phrase came from an open mic.
    via: "name" (hands-free, started with Dieter), "follow-up" (hands-free, just after Dieter spoke),
    or "conversation" (the Tell Dieter mic left open for a conversation)."""
    if via == "conversation":
        return _SPOKEN + _CONVERSATION
    return _SPOKEN + (_FOLLOW_UP if via == "follow-up" else _BY_NAME)

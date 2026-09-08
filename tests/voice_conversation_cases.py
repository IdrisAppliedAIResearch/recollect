"""Synthetic user conversations for the opt-in live voice evaluation."""

from __future__ import annotations


def conversations() -> dict[str, list[dict]]:
    memory = [
        ("rent", "Remember that my monthly rent ceiling is fifteen hundred dollars."),
        (
            "correction",
            "Actually, make that sixteen hundred dollars, not fifteen hundred.",
        ),
        (
            "appointment",
            "Remember my appointment is September eighteenth at four fifteen PM "
            "Central.",
        ),
        ("food", "For meal planning, remember I am vegetarian and dislike mushrooms."),
        ("brief_recall", "What is my corrected rent ceiling?"),
        (
            "topic_switch",
            "Now help me plan a quiet weekend at home. Give me two ideas.",
        ),
        ("referent", "The second idea sounds good. What would I need?"),
        ("cooking", "Suggest a quick dinner using my food preferences."),
        ("cache", "What is a cache, in plain language?"),
        ("analogy", "Give me a different analogy for that."),
        (
            "comparison",
            "How is remembering a fact different from memorizing every word?",
        ),
        ("recipe", "How do I keep pasta from sticking?"),
        ("plants", "Why do house plants lean toward a window?"),
        ("laundry", "How can I keep a black shirt from fading?"),
        ("learning", "Give me one way to practice a new language every day."),
        ("tea", "What is the difference between steeping and boiling tea?"),
        ("writing", "Help me say thank you for lending me a book, casually."),
        ("packing", "Name three useful things for a day hike."),
        ("time", "How many minutes are in two and a half hours?"),
        ("planning", "What is a good first step when a project feels overwhelming?"),
        ("explain", "Why does a metal spoon feel colder than a wooden one?"),
        ("succinct", "Describe a rainbow in one sentence."),
        ("tradeoff", "Compare reading a paper book with listening to an audio book."),
        ("creative", "Suggest a playful name for a tiny orange cat."),
        ("revision", "Make that name shorter."),
        ("measurement", "How many centimeters are in a meter?"),
        ("sequence", "What should I do before painting a small wooden shelf?"),
        ("weather_concept", "What does humidity mean?"),
        ("organization", "Suggest a simple way to organize a messy desk."),
        ("exercise_concept", "What does warming up before a walk mean?"),
        (
            "budget_concept",
            "Explain the difference between a fixed and a variable expense.",
        ),
        (
            "apology",
            "Give me a friendly sentence apologizing for being ten minutes late.",
        ),
        ("no_repeat", "Say something encouraging without repeating your last answer."),
        (
            "decision",
            "What is one question I should ask before buying something I do not need?",
        ),
        ("maps", "Why does a map need a scale?"),
        ("story", "Give me a one sentence story about a lost umbrella."),
        ("battery", "Why does a rechargeable battery wear out eventually?"),
        ("calendar", "How many weeks are in twenty eight days?"),
        ("art", "What is a complementary color?"),
        ("sorting", "How would you sort a mixed box of buttons?"),
        ("sound", "Why does an empty room echo?"),
        ("habit", "Give me a simple way to remember to water a plant."),
        (
            "memory_beyond_recent",
            "What is my corrected rent ceiling, and when is my appointment?",
        ),
        ("preference_beyond_recent", "What food preferences did I tell you earlier?"),
        (
            "update_again",
            "Change my appointment to four thirty PM Central on the same day.",
        ),
        ("confirm_update", "Repeat my appointment date and time, and my rent ceiling."),
    ]
    precision = [
        (
            "currency",
            "In this example, rent is four hundred dollars per month. Say the amount "
            "naturally.",
        ),
        ("cents", "Repeat this exactly: fifty cents per request, not fifty dollars."),
        (
            "foreign_currency",
            "Compare four hundred Canadian dollars with four hundred Australian "
            "dollars without converting them.",
        ),
        (
            "magnitude",
            "Repeat the range four hundred thousand to six hundred thousand dollars.",
        ),
        (
            "annual",
            "An annual budget is one point two million dollars. What is that per "
            "month?",
        ),
        (
            "units",
            "Explain the difference between five megabytes per second and five "
            "megabits per second.",
        ),
        (
            "math",
            "Two times three is six. Two to the power of three is eight. Explain the "
            "difference briefly.",
        ),
        (
            "code",
            "Show me the Python expression for two times three and explain what it "
            "does.",
        ),
        ("no_table", "Compare buying and renting a bicycle for a weekend."),
        (
            "detail",
            "Give me six steps for planning a small dinner party, with an example "
            "and one thing to watch out for.",
        ),
        ("shorten", "Now give me just the most important step."),
        ("negation", "I said do not include mushrooms. Suggest one vegetarian dinner."),
    ]
    clarification = [
        (
            "two_values",
            "Remember my dinner budget is forty dollars and there will be six guests.",
        ),
        ("ambiguous", "Change it to twenty."),
        ("clarify", "I mean the budget. Keep the number of guests the same."),
        ("confirm", "What is the budget and how many guests are coming?"),
        (
            "quote_command",
            "How can I politely ask someone to stop talking during a movie?",
        ),
        (
            "new_session_boundary",
            "Have I told you my rent ceiling in this conversation?",
        ),
    ]
    research = [
        (
            "current_lookup",
            "Look up the current official Austin Central Library opening hours for "
            "today. Tell me when to arrive if I need ninety minutes before closing. "
            "Include the source on screen.",
        ),
        (
            "research_followup",
            "What closing time did you find, and what day does that apply to?",
        ),
        (
            "historical_lookup",
            "Find the official United States Census population of Austin Texas in "
            "twenty twenty. Give me the number and source briefly.",
        ),
    ]
    repair = [
        ("technical_clarification", "I mean a cache in a computer. What is that?"),
        ("repaired_referent", "Give me another analogy for that."),
        (
            "time_explicit",
            "Remember my appointment is at four hours and thirty minutes PM.",
        ),
        ("time_confirmation", "What time is my appointment?"),
        ("time_correction", "Actually the appointment is at five forty five PM."),
        ("time_corrected_recall", "Please repeat the appointment time."),
        (
            "money_precision",
            "The cost is fifty cents per request. That is not fifty dollars.",
        ),
        (
            "quote_full_contrast",
            "Repeat all of these words, including the last part: fifty cents per "
            "request, not fifty dollars.",
        ),
        ("short_decision_prompt", "Ask me whether I want to discuss books or movies."),
        ("short_decision", "Books."),
        ("stop_topic", "No, let us change the topic. Why is the sky blue?"),
        ("scope_control", "Explain it in ten words."),
    ]
    groups = {
        "memory": memory,
        "precision": precision,
        "clarification": clarification,
        "research": research,
        "repair": repair,
    }
    return {
        group: [
            {"id": key, "text": text, "voice": "af_heart", "speed": 1.0}
            for key, text in rows
        ]
        for group, rows in groups.items()
    }

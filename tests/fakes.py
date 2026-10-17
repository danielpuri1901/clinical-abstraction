"""Shared fakes for tests and evals. Nothing here calls a model."""

from clinical_abstraction.jev import Choice, ChoiceAnswer

NEUTRAL = {"attendance": "not_applicable", "service_type": "any", "group_by": "none"}  # a plan field with no value


class FakeJev:
    """Answers Jev choice questions from a fixed table and records every request.

    answers maps a question name to (choice, confidence). A name missing from the table answers its neutral value with
    confidence 1.0. The probabilities put the stated confidence on the choice. Like the real Jev, it can only choose a
    value the question offers.
    """

    def __init__(self, answers: dict[str, tuple[str, float]]):
        self.answers = answers
        self.calls: list[tuple[str, list[str]]] = []  # (state, question names) per request

    def ask(self, state: str, questions: dict[str, Choice]) -> dict[str, ChoiceAnswer]:
        self.calls.append((state, list(questions)))
        replies = {}
        for name, question in questions.items():
            choice, confidence = self.answers.get(name) or (NEUTRAL[name], 1.0)
            assert choice in question.criteria, f"{choice!r} is not offered for {name}"
            replies[name] = ChoiceAnswer(choice, confidence, {choice: confidence})
        return replies

    @classmethod
    def from_expected(cls, expected: dict) -> "FakeJev":
        """A decision model sure of the expected plan: confidence 1.0 on each field, the neutral value where it has none."""
        neutral = {field: (expected.get(field) or value, 1.0) for field, value in NEUTRAL.items()}
        return cls({"metric": (expected["metric"], 1.0), **neutral})

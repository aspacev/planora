"""Which summary points the draft accounts for — and which tasks came from nowhere.

The summary is the list of things a person confirmed. A draft is trustworthy to
the extent that it can be traced back to that list in both directions:

- a point no task cites and the draft does not declare as context is a
  requirement the plan lost;
- a task that cites no point is work nobody asked for.

Both are computed here, from the numbers alone, without asking the model
whether it did a good job: a model that skipped a requirement will also report
that it skipped nothing.

Thesis numbers are 1-based everywhere — in the prompt, in the stored draft and
in the API — because they are shown to a person as "point 3", and a second,
0-based numbering for the machine would be an off-by-one waiting to happen.
"""

from dataclasses import dataclass

from app.ai.schemas import Draft


@dataclass(frozen=True)
class Coverage:
    #: Summary points with no task and not declared as context.
    uncovered: list[int]
    #: Summary points the draft declared as needing no task: a constraint, an
    #: exclusion, a fact about the project.
    context: list[int]
    #: (category index, task index), 0-based — they address rows of the draft
    #: the same way the editing screen does.
    unsourced: list[tuple[int, int]]

    @property
    def clean(self) -> bool:
        return not self.uncovered and not self.unsourced

    @property
    def gaps(self) -> int:
        return len(self.uncovered) + len(self.unsourced)

    def as_json(self) -> dict:
        return {
            "uncovered": self.uncovered,
            "context": self.context,
            "unsourced": [
                {"category": category, "task": task} for category, task in self.unsourced
            ],
        }


def normalize(draft: Draft, theses: int) -> Draft:
    """The draft with only the references that point at a real summary point.

    The model and a person editing by hand can both send a number that is not in
    the summary. Such a reference is dropped rather than refused: the draft is
    otherwise usable, and a task left with no valid reference simply shows up as
    unsourced — which is exactly what it is.

    A point cited by a task is not context, whatever the draft says: "this needs
    no task" and "here is its task" cannot both hold, and the task is the
    stronger evidence.
    """

    def valid(numbers: list[int]) -> list[int]:
        return sorted({number for number in numbers if 1 <= number <= theses})

    for category in draft.categories:
        for task in category.tasks:
            task.sources = valid(task.sources)
    cited = _cited(draft)
    draft.context_theses = [
        number for number in valid(draft.context_theses) if number not in cited
    ]
    return draft


def coverage(draft: Draft, theses: int) -> Coverage:
    cited = _cited(draft)
    context = [number for number in draft.context_theses if number not in cited]
    accounted = cited | set(context)
    return Coverage(
        uncovered=[number for number in range(1, theses + 1) if number not in accounted],
        context=context,
        unsourced=[
            (category_index, task_index)
            for category_index, category in enumerate(draft.categories)
            for task_index, task in enumerate(category.tasks)
            if not any(1 <= number <= theses for number in task.sources)
        ],
    )


def renumber(draft: Draft, before: list[str], after: list[str]) -> Draft:
    """The draft's references moved onto an edited summary.

    References are positions, so deleting point 2 would silently turn every
    reference to point 3 into a reference to a different sentence. A point is
    followed by its text: an untouched point keeps its links under its new
    number, a reworded or deleted one loses them — the claim it made has changed,
    and whether the task still follows from it is for a person to judge.
    """
    position = {text: index for index, text in enumerate(after, start=1)}
    moved = {
        number: position[text]
        for number, text in enumerate(before, start=1)
        if text in position
    }

    def follow(numbers: list[int]) -> list[int]:
        return sorted({moved[number] for number in numbers if number in moved})

    for category in draft.categories:
        for task in category.tasks:
            task.sources = follow(task.sources)
    draft.context_theses = follow(draft.context_theses)
    return draft


def _cited(draft: Draft) -> set[int]:
    return {
        number
        for category in draft.categories
        for task in category.tasks
        for number in task.sources
    }

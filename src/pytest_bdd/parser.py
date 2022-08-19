from __future__ import annotations

import collections
import os.path
import re
import textwrap
import typing
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import cast

from . import exceptions, types

if typing.TYPE_CHECKING:
    from typing import Any, Iterable, Mapping, Match, Sequence

SPLIT_LINE_RE = re.compile(r"(?<!\\)\|")
STEP_PARAM_RE = re.compile(r"<(.+?)>")
COMMENT_RE = re.compile(r"(^|(?<=\s))#")
STEP_PREFIXES = [
    ("Feature: ", types.FEATURE),
    ("Scenario Outline: ", types.SCENARIO_OUTLINE),
    ("Examples:", types.EXAMPLES),
    ("Scenario: ", types.SCENARIO),
    ("Background:", types.BACKGROUND),
    ("Given ", types.GIVEN),
    ("When ", types.WHEN),
    ("Then ", types.THEN),
    ("@", types.TAG),
    # Continuation of the previously mentioned step type
    ("And ", None),
    ("But ", None),
]


class ValidationError(Exception):
    pass


def split_line(line: str) -> list[str]:
    """Split the given Examples line.

    :param str|unicode line: Feature file Examples line.

    :return: List of strings.
    """
    return [cell.replace("\\|", "|").strip() for cell in SPLIT_LINE_RE.split(line)[1:-1]]


def parse_line(line: str) -> tuple[str, str]:
    """Parse step line to get the step prefix (Scenario, Given, When, Then or And) and the actual step name.

    :param line: Line of the Feature file.

    :return: `tuple` in form ("<prefix>", "<Line without the prefix>").
    """
    for prefix, _ in STEP_PREFIXES:
        if line.startswith(prefix):
            return prefix.strip(), line[len(prefix) :].strip()
    return "", line


def strip_comments(line: str) -> str:
    """Remove comments.

    :param str line: Line of the Feature file.

    :return: Stripped line.
    """
    res = COMMENT_RE.search(line)
    if res:
        line = line[: res.start()]
    return line.strip()


def get_step_type(line: str) -> str | None:
    """Detect step type by the beginning of the line.

    :param str line: Line of the Feature file.

    :return: SCENARIO, GIVEN, WHEN, THEN, or `None` if can't be detected.
    """
    for prefix, _type in STEP_PREFIXES:
        if line.startswith(prefix):
            return _type
    return None


def parse_feature(basedir: str, filename: str, encoding: str = "utf-8") -> Feature:
    """Parse the feature file.

    :param str basedir: Feature files base directory.
    :param str filename: Relative path to the feature file.
    :param str encoding: Feature file encoding (utf-8 by default).
    """
    abs_filename = os.path.abspath(os.path.join(basedir, filename))
    rel_filename = os.path.join(os.path.basename(basedir), filename)
    feature = Feature(
        scenarios=OrderedDict(),
        filename=abs_filename,
        rel_filename=rel_filename,
        line_number=1,
        name=None,
        tags=set(),
        background=None,
        description="",
    )
    scenario: ScenarioTemplate | None = None
    mode: str | None = None
    prev_mode = None
    description: list[str] = []
    step = None
    multiline_step = False
    prev_line = None

    with open(abs_filename, encoding=encoding) as f:
        content = f.read()

    for line_number, line in enumerate(content.splitlines(), start=1):
        unindented_line = line.lstrip()
        line_indent = len(line) - len(unindented_line)
        if step and (step.indent < line_indent or ((not unindented_line) and multiline_step)):
            multiline_step = True
            # multiline step, so just add line and continue
            step.add_line(line)
            continue
        else:
            step = None
            multiline_step = False
        stripped_line = line.strip()
        clean_line = strip_comments(line)
        if not clean_line and (not prev_mode or prev_mode not in types.FEATURE):
            continue
        mode = get_step_type(clean_line) or mode

        allowed_prev_mode = (types.BACKGROUND, types.GIVEN, types.WHEN)

        if not scenario and prev_mode not in allowed_prev_mode and mode in types.STEP_TYPES:
            raise exceptions.FeatureError(
                "Step definition outside of a Scenario or a Background", line_number, clean_line, filename
            )

        if mode == types.FEATURE:
            if prev_mode is None or prev_mode == types.TAG:
                _, feature.name = parse_line(clean_line)
                feature.line_number = line_number
                feature.tags = get_tags(prev_line)
            elif prev_mode == types.FEATURE:
                description.append(clean_line)
            else:
                raise exceptions.FeatureError(
                    "Multiple features are not allowed in a single feature file",
                    line_number,
                    clean_line,
                    filename,
                )

        prev_mode = mode

        # Remove Feature, Given, When, Then, And
        keyword, parsed_line = parse_line(clean_line)

        if mode in [types.SCENARIO, types.SCENARIO_OUTLINE]:
            tags = get_tags(prev_line)
            scenario = ScenarioTemplate(
                feature=feature,
                name=parsed_line,
                line_number=line_number,
                tags=tags,
                templated=mode == types.SCENARIO_OUTLINE,
            )
            feature.scenarios[parsed_line] = scenario
        elif mode == types.BACKGROUND:
            feature.background = Background(feature=feature, line_number=line_number)
        elif mode == types.EXAMPLES:
            mode = types.EXAMPLES_HEADERS
            scenario.examples.line_number = line_number
        elif mode == types.EXAMPLES_HEADERS:
            scenario.examples.set_param_names([l for l in split_line(parsed_line) if l])
            mode = types.EXAMPLE_LINE
        elif mode == types.EXAMPLE_LINE:
            scenario.examples.add_example([l for l in split_line(stripped_line)])
        elif mode and mode not in (types.FEATURE, types.TAG):
            step = Step(name=parsed_line, type=mode, indent=line_indent, line_number=line_number, keyword=keyword)
            if feature.background and not scenario:
                feature.background.add_step(step)
            else:
                scenario = cast(ScenarioTemplate, scenario)
                scenario.add_step(step)
        prev_line = clean_line

    feature.description = "\n".join(description).strip()
    return feature


@dataclass
class Feature:
    """Feature."""

    scenarios: OrderedDict[str, ScenarioTemplate]
    filename: str
    rel_filename: str
    name: str | None
    tags: set[str]
    background: Background | None
    line_number: int
    description: str

    def validate(self):
        for scenario_name, scenario in self.scenarios.items():
            if scenario_name != scenario.name:
                raise ValidationError(
                    f"Expected scenario_name and scenario " f"to be the same: {scenario_name} != {scenario.name}"
                )
            scenario.validate()

        if self.background is not None:
            self.background.validate()


@dataclass
class ScenarioTemplate:
    """A scenario template.

    Created when parsing the feature file, it will then be combined with the examples to create a Scenario."""

    feature: Feature
    name: str
    line_number: int
    templated: bool
    tags: set[str] = field(default_factory=set)
    examples: Examples | None = field(default_factory=lambda: Examples())
    _steps: list[Step] = field(init=False, default_factory=list)

    def __post_init__(self):
        if self.examples is None:
            self.examples = Examples()
        if self.tags is None:
            self.tags = set()

    def add_step(self, step: Step) -> None:
        """Add step to the scenario.

        :param pytest_bdd.parser.Step step: Step.
        """
        step.scenario = self
        self._steps.append(step)

    @property
    def steps(self) -> list[Step]:
        background = self.feature.background
        return (background.steps if background else []) + self._steps

    def render(self, context: Mapping[str, Any]) -> Scenario:
        background_steps = self.feature.background.steps if self.feature.background else []
        if not self.templated:
            scenario_steps = self._steps
        else:
            scenario_steps = [
                Step(
                    name=step.render(context),
                    type=step.type,
                    indent=step.indent,
                    line_number=step.line_number,
                    keyword=step.keyword,
                )
                for step in self._steps
            ]
        steps = background_steps + scenario_steps
        return Scenario(feature=self.feature, name=self.name, line_number=self.line_number, steps=steps, tags=self.tags)

    def validate(self):
        if self.feature is None:
            raise ValidationError("Missing feature")
        for step in self._steps:
            if step.background is not None:
                raise ValidationError(
                    f"Step {step} is connected to a background ({step.background}), "
                    f"but it should not since it's part of a scenario ({self})."
                )
            if step.scenario != self:
                raise ValidationError(f"Step {step} is not connected to the scenario {self}")


@dataclass
class Scenario:
    """Scenario."""

    feature: Feature
    name: str
    line_number: int
    steps: list[Step]
    tags: set[str] = field(default_factory=set)

    def __post_init__(self):
        if self.tags is None:
            self.tags = set()


@dataclass
class Step:
    type: str
    _name: str
    line_number: int
    indent: int
    keyword: str
    docstring: Docstring | None = None
    datatable: list[tuple[str, ...]] | None = None
    failed: bool = field(init=False, default=False)
    scenario: ScenarioTemplate | None = field(init=False, default=None)
    background: Background | None = field(init=False, default=None)
    lines: list[str] = field(init=False, default_factory=list)

    def __init__(
        self,
        type: str,
        name: str,
        line_number: int,
        indent: int,
        keyword: str,
        docstring: str | None = None,
        datatable: list[tuple[str, ...]] | None = None,
    ):
        self.type = type
        self.indent = indent
        self.keyword = keyword
        self.docstring = docstring
        self.datatable = datatable
        self.scenario = None
        self.background = None
        self.lines = []

    def add_line(self, line: str) -> None:
        """Add line to the multiple step.

        :param str line: Line of text - the continuation of the step name.
        """
        self.lines.append(line)

    @property
    def name(self) -> str:
        """Get step name."""
        multilines_content = textwrap.dedent("\n".join(self.lines)) if self.lines else ""

        # Remove the multiline quotes, if present.
        multilines_content = re.sub(
            pattern=r'^"""\n(?P<content>.*)\n"""$',
            repl=r"\g<content>",
            string=multilines_content,
            flags=re.DOTALL,  # Needed to make the "." match also new lines
        )

        lines = [self._name] + [multilines_content]
        return "\n".join(lines).strip()

    @name.setter
    def name(self, value: str) -> None:
        """Set step name."""
        self._name = value

    def __str__(self) -> str:
        """Full step name including the type."""
        return f'{self.type.capitalize()} "{self.name}"'

    @property
    def params(self) -> tuple[str, ...]:
        """Get step params."""
        return tuple(frozenset(STEP_PARAM_RE.findall(self.name)))

    def render(self, context: Mapping[str, Any]) -> str:
        def replacer(m: Match):
            varname = m.group(1)
            return str(context[varname])

        return STEP_PARAM_RE.sub(replacer, self.name)


class Docstring(collections.UserString):
    def __init__(self, body: str, content_type: str | None):
        super().__init__(body)
        self.content_type = content_type


@dataclass
class Background:
    feature: Feature
    line_number: int
    steps: list[Step] = field(init=False, default_factory=list)

    def add_step(self, step: Step) -> None:
        """Add step to the background."""
        step.background = self
        self.steps.append(step)

    def validate(self):
        if self.feature is None:
            raise ValidationError(f"Background {self} not connected to a feature.")
        for step in self.steps:
            if step.background != self:
                raise ValidationError(f"Step {step} is not connected to the background {self}")
            if step.scenario is not None:
                raise ValidationError(
                    f"Step {step} is connected to a scenario ({step.scenario}), "
                    f"but it's already part of a background ({self})."
                )


@dataclass
class Examples:
    """Example table."""

    example_params: list[str] = field(default_factory=list)
    examples: list[Sequence[str]] = field(default_factory=list)
    line_number: int | None = field(default=None)
    name: str | None = field(default=None)

    def set_param_names(self, keys: Iterable[str]) -> None:
        """Set parameter names.

        :param names: `list` of `string` parameter names.
        """
        self.example_params = [str(key) for key in keys]

    def add_example(self, values: Sequence[str]) -> None:
        """Add example.

        :param values: `list` of `string` parameter values.
        """
        self.examples.append(values)

    def as_contexts(self) -> Iterable[dict[str, Any]]:
        if not self.examples:
            return

        header, rows = self.example_params, self.examples

        for row in rows:
            assert len(header) == len(row)
            yield dict(zip(header, row))

    def __bool__(self) -> bool:
        """Bool comparison."""
        return bool(self.examples)


def get_tags(line: str | None) -> set[str]:
    """Get tags out of the given line.

    :param str line: Feature file text line.

    :return: List of tags.
    """
    if not line or not line.strip().startswith("@"):
        return set()
    return {tag.lstrip("@") for tag in line.strip().split(" @") if len(tag) > 1}
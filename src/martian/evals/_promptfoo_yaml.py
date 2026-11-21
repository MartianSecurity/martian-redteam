"""The YAML 1.2 scalar rules shared by Promptfoo config and embedded object parsing."""

from __future__ import annotations

import re

import yaml
from yaml.nodes import MappingNode, ScalarNode

_BOOL = "tag:yaml.org,2002:bool"
_INT = "tag:yaml.org,2002:int"
_FLOAT = "tag:yaml.org,2002:float"
_SURROGATE = re.compile(r"[\ud800-\udfff]")


class CoreYamlLoader(yaml.SafeLoader):
    yaml_implicit_resolvers = {
        char: [(tag, expression) for tag, expression in rules if tag not in {_BOOL, _INT, _FLOAT}]
        for char, rules in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }

    def construct_scalar(self, node: ScalarNode | MappingNode) -> str:
        value = super().construct_scalar(node)
        if not _SURROGATE.search(value):
            return value
        try:
            # JSON syntax inside YAML can spell a supplementary character as two escapes.
            return value.encode("utf-16-le", errors="surrogatepass").decode("utf-16-le")
        except UnicodeError:
            raise yaml.constructor.ConstructorError(None, None, "unpaired Unicode surrogate", node.start_mark) from None


def _integer(loader: CoreYamlLoader, node: ScalarNode) -> int:
    value = loader.construct_scalar(node)
    return int(value, 0 if value.lstrip("+-").startswith(("0o", "0x")) else 10)


CoreYamlLoader.add_implicit_resolver(_BOOL, re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), "tTfF")
CoreYamlLoader.add_implicit_resolver(_INT, re.compile(r"^(?:[+-]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$"), "-+0123456789")
CoreYamlLoader.add_implicit_resolver(
    _FLOAT,
    re.compile(r"^(?:[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|[+-]?\.(?i:inf)|\.(?i:nan))$"),
    "-+.0123456789",
)
CoreYamlLoader.add_constructor(_INT, _integer)

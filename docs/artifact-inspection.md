# Static tool and skill inspection

`martian-redteam inspect-artifact` inspects an explicitly supplied local artifact.
It reads at most 256 KiB, executes no content, follows no linked resources and
contacts no model or hosted service. Install the `mcp` extra for JSON Schema checking.

```sh
uv run --extra mcp martian-redteam inspect-artifact tool.json --kind mcp_tool
uv run --extra mcp martian-redteam inspect-artifact SKILL.md --kind skill
```

MCP JSON has exactly `name`, `description` and `input_schema`. Skill input is UTF-8
text. A report pins the canonical MCP JSON or exact skill bytes with SHA-256 and
`martian-static-metadata-v2`. It includes rule IDs, JSON-pointer locations and readable
reasons, but no copied descriptions or matched text. Structure and field names can
still be sensitive; keep private reports in the customer environment.

Checks cover object argument schemas, JSON Schema 2020-12 validity, external schema
references, open root argument properties, bounded instruction/concealment language
and invisible direction controls. Schema traversal is bounded to 4096 nodes and 32
levels before recursive JSON Schema validation; the full artifact has an iterative
8,192-node/64-level preflight before JSON encoding. Oversized strings, cycles and
unsupported Python values fail within these bounds. Up to 128 findings are retained
with a limit indicator. Out-of-bounds or
unreadable input fails explicitly. External references are reported without retrieval.
Skill frontmatter, referenced files, executable hooks, dependencies, source authenticity
and runtime behavior are not evaluated by this version.

`invalid` means a structural error; CLI exit 2 also reports malformed inputs or failed
inspection. `review_required` is a triage signal, not a maliciousness label. Benign
quotes, examples and guidance can match text patterns. `no_signal` means no supported
pattern matched, never a clean bill of health. Both successful triage states exit 0;
automated approval must not treat that exit code as a security verdict.

MCP binding runs the same inspection over each selected tool's full supplied metadata
before shortening the description for the model. Invalid object schemas stop binding;
review signals do not silently add a new runtime block. `BoundMCPTools.inspections`
exposes the pinned reports under local tool aliases and optional customer traces record
`tool.inspected`. SDK reports remain local and are not part of runtime telemetry uploads.
An administrator may separately submit source content through the
[Discovery artifact inspection API](../../backend/docs/discovery.md#submitted-artifact-inspection);
that explicit upload is not enabled by SDK reporting. The backend discards submitted
content after scanning and retains only its bounded report, digest and declarations.
The backend's direct-MCP connection validation mirrors this scanner and stores its
report with each discovered capability. The approval screen shows that exact report;
changing the metadata changes the connection revision and requires the existing
validation/approval workflow. It does not prove a remote server preserves that version.

## Container and Kubernetes use

```sh
docker build -f Dockerfile.inspection -t martian-inspection:local .
docker run --rm --network none --read-only --tmpfs /tmp \
  -v "$PWD/tool.json:/input/tool.json:ro" martian-inspection:local \
  /input/tool.json --kind mcp_tool
```

The image runs as UID 10001 and needs no credentials or outbound network. The local
[Job manifest](../infra/kubernetes-artifact-inspection.yaml) expects a non-sensitive
`inspection-input` ConfigMap containing `tool.json`. In Kubernetes,
mount the chosen artifact read-only into the same image, disable service-account token
automount, use a read-only root filesystem and `/tmp` emptyDir, and set explicit CPU,
memory, deadline and `backoffLimit: 0`. Use a Secret or protected volume for private
artifacts; a ConfigMap is appropriate only for non-sensitive metadata. Do not copy
private customer source into shared reports or model training data.

The scanner is evidence for review. Contextual learned analysis and independently
verified behavioral scanning remain separate work in the suite plan.

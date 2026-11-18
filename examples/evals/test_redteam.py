"""Include redteam in the same CI session as dataset and quality evals.

Run: martian evals run tests/evals
One Backend origin and API token with evals/CI and redteam:run capabilities are required.
"""

import asyncio

import mlflow

from martian.evals import assert_redteam
from martian.redteam import Client, PromptInjection, PromptLeakage, a_red_team


@mlflow.test
def test_prompt_leakage():
    from my_project.agent import answer

    async def assess():
        async with Client() as client:
            return await a_red_team(
                target=answer,
                client=client,
                purpose="Customer support assistant",
                vulnerabilities=[PromptLeakage(types=["system_prompt"])],
                attacks=[PromptInjection()],
                max_concurrent=1,
                attacks_per_vulnerability_type=1,
            )

    assessment = asyncio.run(assess())
    assessment.save("results/redteam/prompt-leakage")
    assert_redteam(assessment)

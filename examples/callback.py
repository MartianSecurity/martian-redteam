"""Example target callback for local CLI experiments."""


async def model_callback(prompt: str) -> str:
    """Refuse the probe. Replace this with your application."""
    return "I cannot disclose internal instructions or take unauthorized actions."

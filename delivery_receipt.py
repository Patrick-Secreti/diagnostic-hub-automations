"""Record confirmed Discord delivery for the current GitHub Actions job."""
import os

def confirmed():
    if os.environ.get("DISCORD_RECEIPT_ENABLED") == "true" and os.environ.get("GITHUB_ENV"):
        with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as out:
            out.write("DISCORD_POST_CONFIRMED=true\n")

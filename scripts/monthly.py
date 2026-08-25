import json
from pathlib import Path

from fastcore.script import call_parse

from wiki2dataset.release import monthly_release


@call_parse
def main(
    path:Path=Path("data"), # Working data directory
    workers:int=12, # Download and conversion workers
    upload:bool=True, # Publish completed packages
    checkpoint:str=None, # Persistent Hugging Face bucket URI
):
    "Build and publish the monthly SimpleWiki and English Wikipedia datasets."
    print(json.dumps(monthly_release(path, workers, upload, checkpoint=checkpoint), indent=2, default=str))

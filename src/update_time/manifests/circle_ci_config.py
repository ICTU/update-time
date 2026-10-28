"""Read the machine-executor images of CircleCI configs."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from update_time.formats.yaml import Document


def machine_images(document: Document) -> set[str]:
    """Return the machine-executor image references (the `image:` under any `machine:` key) in a parsed config."""
    return {image for node in document.nodes() if (image := node["machine"]["image"].string()) is not None}

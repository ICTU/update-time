"""Read the images Docker Compose files build."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from update_time.formats.yaml import Document


def built_images(document: Document) -> set[str]:
    """Return the images the Compose file builds: the `image:` of each service that also declares a `build:`."""
    return {
        image
        for service in document["services"].values()
        if "build" in service and (image := service["image"].string()) is not None
    }

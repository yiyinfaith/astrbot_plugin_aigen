"""Configurable text-to-image and image-to-image directions."""

from .video_router import VideoError


def select_image_route(config: dict, images: list, model: str) -> dict:
    """Resolve image routing without changing the existing default model.

    Args:
        config: Image settings.
        images: Already resolved input images.
        model: Existing command or tool model selection.

    Returns:
        An independent effective configuration.

    Raises:
        VideoError: If the chosen route is missing or duplicated.
    """
    from .generation_capabilities import IMAGE, parameter_defaults, supplied

    effective = {**config, **parameter_defaults(config), "model": model}
    if not config.get("auto_route", True) or "routes" not in config:
        return effective
    route = "image_to_image" if images else "text_to_image"
    label = "图生图" if images else "文生图"
    matches = [
        r
        for r in config["routes"]
        if isinstance(r, dict) and r.get("__template_key") == route
    ]
    if len(matches) != 1:
        raise VideoError(f"请在生图自动路由中为{label}配置恰好一条路线。")
    for key in ("model", "interface_mode", "base_url", "api_keys"):
        value = matches[0].get(key)
        if value and value != "inherit":
            effective[key] = value
    effective.update(
        supplied({key: matches[0][key] for key in IMAGE if key in matches[0]})
    )
    effective.update(parameter_defaults(matches[0]))
    return effective

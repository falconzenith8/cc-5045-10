"""Review scenes: small / medium / complex.

A config version fully pins scene, camera and materials, so every produced
frame is bound to a complete immutable configuration.
"""


def _ground(albedo=(0.35, 0.35, 0.38)):
    return {
        "type": "plane",
        "point": (0, 0, 0),
        "normal": (0, 1, 0),
        "material": {"type": "lambert", "albedo": albedo},
    }


def small():
    """Simple: one matte sphere, one light, low primitive count."""
    return {
        "name": "simple_sphere",
        "sky_top": (0.18, 0.25, 0.42),
        "sky_bottom": (0.55, 0.55, 0.6),
        "primitives": [
            _ground(),
            {
                "type": "sphere",
                "center": (0, 1.0, 0),
                "radius": 1.0,
                "material": {"type": "lambert", "albedo": (0.8, 0.25, 0.2)},
            },
            {
                "type": "sphere",
                "center": (0, 3.6, -1.2),
                "radius": 1.1,
                "material": {"type": "emissive", "emission": (6.0, 5.6, 4.8)},
            },
        ],
    }


def medium():
    """Medium: several spheres, metal + matte, area light."""
    prims = [
        _ground((0.3, 0.3, 0.33)),
        {
            "type": "sphere",
            "center": (-1.6, 0.9, 0.4),
            "radius": 0.9,
            "material": {"type": "metal", "albedo": (0.85, 0.85, 0.9), "roughness": 0.08},
        },
        {
            "type": "sphere",
            "center": (0.2, 0.7, -0.6),
            "radius": 0.7,
            "material": {"type": "lambert", "albedo": (0.2, 0.55, 0.85)},
        },
        {
            "type": "sphere",
            "center": (1.7, 1.2, 0.8),
            "radius": 1.2,
            "material": {"type": "lambert", "albedo": (0.9, 0.75, 0.3)},
        },
        {
            "type": "sphere",
            "center": (0, 4.2, -1.5),
            "radius": 1.4,
            "material": {"type": "emissive", "emission": (7.0, 6.5, 5.5)},
        },
    ]
    return {
        "name": "materials_room",
        "sky_top": (0.1, 0.12, 0.2),
        "sky_bottom": (0.4, 0.4, 0.45),
        "primitives": prims,
    }


def complex_scene():
    """Complex: many spheres including glass, dense interreflection."""
    prims = [
        _ground((0.22, 0.22, 0.25)),
        {
            "type": "sphere",
            "center": (-2.4, 0.8, -0.5),
            "radius": 0.8,
            "material": {"type": "metal", "albedo": (0.95, 0.6, 0.25), "roughness": 0.15},
        },
        {
            "type": "sphere",
            "center": (-0.8, 0.6, 1.0),
            "radius": 0.6,
            "material": {"type": "lambert", "albedo": (0.75, 0.2, 0.7)},
        },
        {
            "type": "sphere",
            "center": (0.9, 0.9, 0.2),
            "radius": 0.9,
            "material": {"type": "dielectric", "albedo": (0.95, 1.0, 0.98), "ior": 1.5},
        },
        {
            "type": "sphere",
            "center": (2.5, 1.3, -0.8),
            "radius": 1.3,
            "material": {"type": "lambert", "albedo": (0.25, 0.7, 0.35)},
        },
        {
            "type": "sphere",
            "center": (0, 0.5, -2.2),
            "radius": 0.5,
            "material": {"type": "metal", "albedo": (0.8, 0.85, 0.9), "roughness": 0.02},
        },
        {
            "type": "sphere",
            "center": (-1.8, 0.35, 2.2),
            "radius": 0.35,
            "material": {"type": "emissive", "emission": (3.0, 1.2, 4.0)},
        },
    ]
    for i in range(6):
        prims.append(
            {
                "type": "sphere",
                "center": (-2.5 + i, 0.18, -3.2),
                "radius": 0.18,
                "material": {"type": "lambert",
                             "albedo": (0.2 + 0.12 * i, 0.5, 0.8 - 0.1 * i)},
            }
        )
    prims.append(
        {
            "type": "sphere",
            "center": (0.5, 4.4, -1.0),
            "radius": 1.5,
            "material": {"type": "emissive", "emission": (8.0, 7.5, 6.5)},
        }
    )
    return {
        "name": "glass_and_friends",
        "sky_top": (0.06, 0.08, 0.15),
        "sky_bottom": (0.3, 0.3, 0.36),
        "primitives": prims,
    }


SCENES = {
    "simple_sphere": small,
    "materials_room": medium,
    "glass_and_friends": complex_scene,
}

DEFAULT_CAMERA = {
    "pos": (4.2, 2.6, 5.2),
    "target": (0, 0.9, 0),
    "fov": 45,
    "up": (0, 1, 0),
}


def build_config(scene_name, camera=None, material_overrides=None,
                 width=160, height=120, target_spp=8, job_kind="preview"):
    """Freeze a full, hashable configuration."""
    scene = SCENES[scene_name]()
    camera = dict(DEFAULT_CAMERA if camera is None else camera)
    camera["pos"] = tuple(camera["pos"])
    camera["target"] = tuple(camera["target"])
    camera["up"] = tuple(camera.get("up", (0, 1, 0)))
    overrides = material_overrides or {}
    for key, patch in overrides.items():
        idx, field = key.split(".", 1)
        mat = scene["primitives"][int(idx)]["material"]
        if field in ("albedo", "emission"):
            mat[field] = tuple(patch)
        else:
            mat[field] = patch
    return {
        "scene_name": scene_name,
        "scene": scene,
        "camera": camera,
        "width": int(width),
        "height": int(height),
        "target_spp": int(target_spp),
        "job_kind": job_kind,
    }

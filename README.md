# AnyModel Blender Add-on — DevBuild

GitHub repository for the **AnyModel Avatar Tools** Blender add-on. This repository contains the Blender add-on source, the installable ZIP and development builds. The Minecraft mod JAR is distributed separately on CurseForge and is intentionally not included here.

## Current add-on

- Add-on: **AnyModel Avatar Tools 2.5.2**
- Blender: **4.2 or newer**
- Minecraft content format: `.mcavatar` for avatars and `.avanim` for animation actions
- Installable package: [`dist/AnyModel_Blender_Addon-2.5.2.zip`](dist/AnyModel_Blender_Addon-2.5.2.zip)
- Full Russian guide: [`docs/GUIDE_RU.md`](docs/GUIDE_RU.md)

## Install in Blender

1. Download the ZIP from `dist/` (or download the latest `AnyModel-Blender-Addon-dev` artifact from GitHub Actions).
2. In Blender, open **Edit → Preferences → Add-ons → Install from Disk…**.
3. Select the ZIP. Enable **AnyModel Avatar Tools**.
4. In a 3D View, press **N** and open the **AnyModel** sidebar tab.

Do not unpack the installable ZIP before selecting it in Blender.

## Development builds

Every push to `main`, and every manual run from the **Actions** tab, packages `addon/avatar_action_bridge/` as an installable ZIP and uploads it as a GitHub Actions artifact named `AnyModel-Blender-Addon-dev`.

To update the included stable ZIP after changing the source, package the `avatar_action_bridge` folder itself so it is the ZIP's top-level folder. Keep the same layout as the ZIP in `dist/`.

## Repository contents

- `addon/avatar_action_bridge/` — add-on Python source and bundled AVANIM templates/notices.
- `dist/` — installable Blender add-on ZIP.
- `docs/` — Russian setup guide, format notes and retained upstream changelog.
- `.github/workflows/dev-build.yml` — automatic development artifact packaging.

## Credits and licensing

The add-on's `bl_info` currently identifies Codex as author. Some included material has its own notice, such as the CATS-derived MIT notice and the Quaternius UAL1 CC0 notice; keep those files with the package. No blanket reuse license is declared for the complete AnyModel add-on in this repository.

## Not included

Minecraft `.jar` files, Minecraft installations, user avatars, `.blend` source projects, caches, logs, account credentials and private development files are excluded. The Minecraft JAR is supplied separately through CurseForge.

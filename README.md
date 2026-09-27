# silksong-web-patch

A patch for the **Hollow Knight: Silksong** web port.
No game files are included. The only game-derived content is about 200 KB of replacement texture data in `patch/delta.bin.xz`. You apply the patch to an unmodified copy of the web port with `patch.py`.

## What it changes

| Change | Details |
| --- | --- |
| No more 2 GB unzip in every browser | The original `index.html` downloads all 100 parts of `WebGL.zip`, unpacks them with JSZip and stores them in Cache Storage. The patch unpacks them once into `StreamingAssets/aa/WebGL/`, and the bundles are then served as plain files. Unity loads only the bundles it needs and caches them in IndexedDB. |
| Local files only | `index.html` loads the game files and the Unity loader from its own folder instead of an external CDN. It also deletes the ~2.5 GB zip cache left behind by the old page. |
| Title logo centred over the menu | `LogoTitle` localPosition.x changes from 14.6 to 15.93 in `packed-scenes-menu_title` |
| Title logo textures | Two textures in `packed-sprites-ui` are replaced |
| Unused files removed | `WebGL.zip.part1`–`100` (after extraction), `jszip.js`, `AddressablesLink/link.xml` (build-time only) and TemplateData images that nothing references |

Unity rejects a bundle whose CRC doesn't match the Addressables `catalog.bin`. So after editing a bundle, the patch updates its CRC there as well.

## Usage

Requirements: Python 3.8+ (standard library only) and about 2.1 GB of free space for the extraction. The zip parts are deleted afterwards.

```sh
python patch.py <web-port-folder> --check   # verify only, changes nothing
python patch.py <web-port-folder>

python -m http.server 8000 -d <web-port-folder>   # then open http://localhost:8000/
```

- **Checks:**
  - The patch targets one specific version of the web port.
  - Every input file is verified by SHA-256 before anything is written. If the version differs or a file was modified, the patch stops with an error and changes nothing.
  - Extracted and edited files are verified too.
  - Running the patch again is harmless, even after the zip parts have been deleted.
- **Hosting:** the patched folder works on any static host, e.g. Cloudflare R2 or `python -m http.server`. The largest bundle is about 500 MB, so hosts with a per-file limit of 100 MB (such as GitHub Pages) can't serve it.

## License

The scripts and the patch description are released under the MIT License (see `LICENSE`). Hollow Knight: Silksong and its assets belong to Team Cherry. None of them are included here, apart from the small amount of replacement texture data the patch writes.

These optional tools maintain documentation assets. They are not needed to run the simulator.

- `extract_map.py --source <research-data-directory>` rebuilds the offline OSM background from the documented research source files.
- `capture_ui.py` records real screenshots from the local web interface and encodes the two GIFs. Start the simulator on port 8765 first. This tool needs Pillow and the [Playwright CLI](https://github.com/microsoft/playwright-cli); use `--browser-cli <executable>` if it is not on your path.

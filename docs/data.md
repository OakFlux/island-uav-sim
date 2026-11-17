# Bundled data and provenance

The installed package includes approximately 1.2 MB of prepared research input, plus the browser map. It can run without access to the original research workspace or a weather API. Its purpose is to make the simulation executable and reproduce **two selected historical configurations**; it does not contain all experiment logs.

| Package path, under `island_uav/data` | Content |
| --- | --- |
| `networks/*_seed*.json` | Four network sizes × three demand seeds; physical locations, bases, profiles, aircraft overlay, task templates and task requirements |
| `weather/ifs_20250404.json` | Six representative regions, 97 hourly intervals each, from the held-out historical ECMWF IFS wind block |
| `weather/era5_20250404.json` | Single representative region, 97 hourly intervals, from the compact case's ERA5-derived block |
| `weather/*_development_spells.json` | Complete uncensored opening durations prepared from 2023–2024, by region and service threshold |
| `weather/wind_coordinate_frames.json` | Frozen weather representatives, meridian convergence and coordinate-frame checks |
| `references/*_input.json` | Unchanged assembled archived inputs for one compact and one three-base case |
| `references/index.json` | Archived run IDs, source hashes, expected metrics and declared reproduction scope |
| `display_coordinates.json` | Precomputed geographic map coordinates corresponding to the metric positions |
| `provenance.json` | Sources, licenses, selected-file hashes and preparation scope |

## Geographic and operational assumptions

The expanded networks use nested sets of 48, 96 and 192 candidate observation units on Pingtan Island and three fixed bases. The compact network has 24 locations and one base. Both retain the original research geometry, aircraft profiles, RGB/zoom capabilities and S/L/M task templates. Base locations, charging resources, task requirements and vehicle parameters are experimental assumptions. Candidate observation units derived from mapped geography should not be read as confirmed facilities requiring inspection, an operator's equipment inventory, or permission to fly.

The expanded cases allow straight-line travel between all positions; this is a research routing geometry. The compact case preserves its original 546 directed allowed links, rather than silently replacing them with a complete graph. Varying the fleet size selects prepared aircraft equally from each base while preserving its ordered profile sequence.

Map display uses precomputed EPSG:4326 longitude/latitude. Physics uses the unchanged EPSG:32650 metric coordinates. Display conversion does not modify physical lengths, graph membership or weather-zone assignment. The UI uses the supplied OpenStreetMap-derived geographic layers and works offline; its attribution remains visible.

## Historical wind versus synthetic demonstration wind

Historical data cover **2025-04-04 00:00 UTC to 2025-04-08 01:00 UTC**. The compact product is ERA5-derived; the expanded product is ECMWF IFS at six regional representatives. They are distinct products and should not be treated as the same small-scale meteorological measurement. Hourly regional wind is an operating-environment proxy; it is not CFD, a microscale turbulent flow field or measured wind at every task location.

The historical block stores **true geographic east/north** components. Before execution, the adapter rotates them into the projected grid axes at each original weather product representative. The frozen table supplies meridian convergence. The rotation preserves physical wind speed; projection scale is not applied to aircraft limits or measured wind magnitude. Frame markers reject accidental second rotation. Archived reference inputs already contain the rotated components and are loaded directly.

The default demonstration creates deterministic synthetic regional wind directly in projected grid axes. It is clearly identified as synthetic in the output and UI, and is not labeled as an observed historical case or a formal paper experiment. The optional continuous/fragmented demonstrations change opening duration under an illustrative shared pattern; they are not the paper's full controlled experiment suite.

The B2 rule's opening-spell samples were derived from 2023–2024 and are bundled as prepared values. They exclude left/right-censored spells and are separate from the 2025 evaluation block. The full original two-year raw weather archive is not included. The reference input includes the exact samples used by that execution.

## Attribution and licenses

Code is MIT licensed. Geographic derivatives and underlying source data retain their own licenses; the code license does not replace them.

- **© OpenStreetMap contributors**, [Open Database License 1.0 and attribution requirements](https://www.openstreetmap.org/copyright). Regional geographic extracts were obtained through [Geofabrik](https://download.geofabrik.de/asia/china.html). OSM is used as geographic evidence, rather than a verified inspection-demand register.
- Historical weather was obtained through the [Open-Meteo Historical Weather API](https://open-meteo.com/en/docs/historical-weather-api) and [Historical Forecast API](https://open-meteo.com/en/docs/historical-forecast-api), attributed under CC BY 4.0. ERA5/Copernicus and ECMWF source attribution also applies. Prepared input should be cited together with the paper; API access is not needed to execute the bundled examples.

`provenance.json` records original preparation sources and hashes of exact selected inputs. Original relative research paths identify derivation; they are not runtime dependencies. The archived reference index records the valid execution freeze and selected result hashes for provenance. Matching metrics are checked numerically with tight floating-point tolerances, not by promising cross-platform byte-identical JSON.

/** The bundled example assets from `examples/`, surfaced in the prompt
 * panel's "load an example" dropdown and the preset gallery. Kept in sync
 * with that folder; the Python test_examples.py guards that every file there
 * stays valid. */
import type { AssetSpec } from "./types";
import streetLight from "../../examples/street_light.json";
import parkBench from "../../examples/park_bench.json";
import bikeRack from "../../examples/bike_rack.json";
import planter from "../../examples/planter.json";
import pergola from "../../examples/pergola.json";

export interface ExampleAsset {
  id: string;
  label: string;
  /** One-line description shown on the preset gallery card. */
  desc: string;
  /** Emoji shown on the gallery card (purely decorative). */
  emoji: string;
  spec: AssetSpec;
}

export const EXAMPLE_ASSETS: ExampleAsset[] = [
  {
    id: "street_light",
    label: "Cobra-head street light",
    desc: "Tapered steel pole, swept mast arm, lofted luminaire head.",
    emoji: "🛣️",
    spec: streetLight as unknown as AssetSpec,
  },
  {
    id: "park_bench",
    label: "Slatted park bench",
    desc: "Timber slats bolted to a cast-iron frame on rails.",
    emoji: "🪑",
    spec: parkBench as unknown as AssetSpec,
  },
  {
    id: "bike_rack",
    label: "Inverted-U bike rack",
    desc: "An arrayed row of welded steel hoops at grade.",
    emoji: "🚲",
    spec: bikeRack as unknown as AssetSpec,
  },
  {
    id: "planter",
    label: "Cast-iron urn planter",
    desc: "A lathe-turned vase profile with a soil basin.",
    emoji: "🪴",
    spec: planter as unknown as AssetSpec,
  },
  {
    id: "pergola",
    label: "Post-and-beam pergola",
    desc: "Timber posts carrying beams and a rafter array.",
    emoji: "🏛️",
    spec: pergola as unknown as AssetSpec,
  },
];

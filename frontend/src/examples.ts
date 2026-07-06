/** The bundled example assets from `examples/`, surfaced in the prompt
 * panel's "load an example" dropdown. Kept in sync with that folder; the
 * Python test_examples.py guards that every file there stays valid. */
import type { AssetSpec } from "./types";
import streetLight from "../../examples/street_light.json";
import parkBench from "../../examples/park_bench.json";
import bikeRack from "../../examples/bike_rack.json";
import planter from "../../examples/planter.json";

export interface ExampleAsset {
  id: string;
  label: string;
  spec: AssetSpec;
}

export const EXAMPLE_ASSETS: ExampleAsset[] = [
  { id: "street_light", label: "Cobra-head street light", spec: streetLight as unknown as AssetSpec },
  { id: "park_bench", label: "Slatted park bench", spec: parkBench as unknown as AssetSpec },
  { id: "bike_rack", label: "Inverted-U bike rack", spec: bikeRack as unknown as AssetSpec },
  { id: "planter", label: "Cast-iron urn planter", spec: planter as unknown as AssetSpec },
];

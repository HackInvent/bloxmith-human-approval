/** Bind release-owned settings through the public action API. */
import { enhanceProperties } from "./properties.js";
import { bindSettings } from "./settings.js";
import { bindReview } from "./review.js";
export function mount(root, api) {
  const disposeStyle = enhanceProperties(root), disposeForm = bindSettings(root, api);
  const disposeReview = bindReview(root, api);
  return {dispose() {disposeReview(); disposeForm(); disposeStyle();}};
}

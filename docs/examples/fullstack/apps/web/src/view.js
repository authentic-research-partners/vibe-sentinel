import { formatGreeting } from "./format.js";

export function renderGreeting(output, greeting) {
  output.textContent = formatGreeting(greeting);
}

import { fetchGreeting } from "./request.js";
import { renderGreeting } from "./view.js";

const output = document.querySelector("#greeting");
try {
  renderGreeting(output, await fetchGreeting());
} catch (error) {
  output.textContent = `Start both local services first. ${error.message}`;
}

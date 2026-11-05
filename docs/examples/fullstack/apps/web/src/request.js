export async function fetchGreeting() {
  const response = await fetch("/api/greeting");
  if (!response.ok) throw new Error(`API returned ${response.status}`);
  return response.json();
}

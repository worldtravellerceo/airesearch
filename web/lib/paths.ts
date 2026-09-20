/** The GitHub Pages sub-path, for links and fetches the framework does not
 *  rewrite for us. Empty in local development. */
export const BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

export function dataUrl(...parts: string[]): string {
  return `${BASE_PATH}/data/${parts.join("/")}`;
}

export function repoSlug(fullName: string): { owner: string; name: string } {
  const [owner, ...rest] = fullName.split("/");
  return { owner, name: rest.join("/") };
}

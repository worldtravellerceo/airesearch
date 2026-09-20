/** @type {import('next').NextConfig} */

// A GitHub Pages project site is served from /<repo>, so every asset and link
// needs that prefix. It is read from the environment because the same code also
// has to run at the root in local development.
const basePath = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

const nextConfig = {
  output: "export",
  basePath,
  trailingSlash: true,
  reactStrictMode: true,
  images: { unoptimized: true },
};

export default nextConfig;

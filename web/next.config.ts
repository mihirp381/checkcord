import type { NextConfig } from "next";
import { version } from "./package.json";

const nextConfig: NextConfig = {
  output: "standalone",
  // Inlined at build time so the browser gets the version without the rest of package.json.
  env: { CHECKCORD_VERSION: version },
};

export default nextConfig;

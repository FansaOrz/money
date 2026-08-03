import type { NextConfig } from "next";

const apiProxyUrl = (process.env.API_PROXY_URL || "http://127.0.0.1:8001").replace(/\/+$/, "");

const nextConfig: NextConfig = {
  reactStrictMode: true,
  output: "standalone",
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${apiProxyUrl}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;

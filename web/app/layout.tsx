import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Trigger Ladder",
  description:
    "VA IRRRL and FHA Streamline opportunity monitor: public rate data and a modeled loan pool.",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  colorScheme: "light",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        {/* Embed mode before first paint: only exactly ?embed=true. */}
        <script
          dangerouslySetInnerHTML={{
            __html:
              "try{if(new URLSearchParams(location.search).get('embed')==='true')document.documentElement.setAttribute('data-embed','')}catch(e){}",
          }}
        />
      </head>
      <body>{children}</body>
    </html>
  );
}

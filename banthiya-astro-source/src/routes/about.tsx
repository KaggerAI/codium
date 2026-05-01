import { createFileRoute } from "@tanstack/react-router";
import { SiteHeader } from "@/components/SiteHeader";
import { SiteFooter } from "@/components/SiteFooter";

export const Route = createFileRoute("/about")({
  head: () => ({
    meta: [
      { title: "About Us — Banthiya Astro Predictions" },
      { name: "description", content: "Learn about Banthiya Astro Predictions and our approach to mundane Vedic astrology." },
      { property: "og:title", content: "About Us — Banthiya Astro Predictions" },
      { property: "og:description", content: "Our approach to mundane Vedic astrology." },
    ],
  }),
  component: About,
});

function About() {
  return (
    <div className="min-h-screen flex flex-col bg-background">
      <SiteHeader />
      <main className="flex-1 mx-auto w-full max-w-4xl px-6 py-16">
        <h2 className="text-4xl font-semibold">About Us</h2>
        <p className="mt-6 text-lg text-muted-foreground leading-relaxed">
          Banthiya Astro Predictions publishes research-grade mundane astrology reports
          covering geopolitics, world economies, financial markets, and global events.
          Our work blends classical Vedic principles with disciplined observation.
        </p>
        <p className="mt-4 text-muted-foreground leading-relaxed">
          Use the homepage to access the latest published reports across our four research categories.
        </p>
      </main>
      <SiteFooter />
    </div>
  );
}

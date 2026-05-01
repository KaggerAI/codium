import { createFileRoute } from "@tanstack/react-router";
import { SiteHeader } from "@/components/SiteHeader";
import { SiteFooter } from "@/components/SiteFooter";
import { PredictionSection, type Report } from "@/components/PredictionSection";
import { AdvertisementBanner } from "@/components/AdvertisementBanner";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "Banthiya Astro Predictions — Mundane Vedic Astrology Reports" },
      {
        name: "description",
        content:
          "Mundane astrology predictions covering geopolitics, world economies, stock & commodity markets, and global events.",
      },
      { property: "og:title", content: "Banthiya Astro Predictions" },
      {
        property: "og:description",
        content: "Vedic mundane astrology reports on geopolitics, economies, and markets.",
      },
    ],
  }),
  component: Index,
});

const geoPolitical: Report[] = [
  { title: "US–Iran War Prediction — Daywise & Month-wise (Apr–Dec 2026)", url: "/reports/US_IRAN_War_Prediction_Daywise_Month_wise.html", date: "2026" },
  { title: "Predictions Scorecard — Day-wise (14th – 22nd Apr)", url: "/reports/Predictions_Scorecard_Day_Wise_14th_-_22nd_Apr.html", date: "April 2026" },
];

const countryEconomy: Report[] = [
  { title: "India — Mundane Astrology 2026", url: "/reports/India_Mundane_Astrology_2026.pdf", date: "2026" },
  { title: "Japan — Mundane Astrology 2026", url: "/reports/Japan_Mundane_Astrology_2026.pdf", date: "2026" },
  { title: "USA — Mundane Astrology 2026", url: "/reports/USA_Mundane_Astrology_2026.pdf", date: "2026" },
];

const stockMarket: Report[] = [
  { title: "India Banking Intelligence Report — April 2026", url: "/reports/India_Banking_Intelligence_Report_April2026.pdf", date: "April 2026" },
  { title: "Fertilizer Intelligence Report", url: "/reports/Fertilizer_Intelligence_Report.html", date: "2026" },
];

const otherMundane: Report[] = [
  { title: "Sample Report 1", url: "#" },
  { title: "Sample Report 2", url: "#" },
  { title: "Sample Report 3", url: "#" },
  { title: "Sample Report 4", url: "#" },
];

function Index() {
  return (
    <div className="min-h-screen flex flex-col bg-background">
      <SiteHeader />
      <main className="flex-1 mx-auto w-full max-w-6xl px-6 py-12">
        <div className="text-center mb-10">
          <h2 className="text-3xl md:text-4xl font-semibold">Latest Predictions & Reports</h2>
          <p className="mt-3 text-muted-foreground max-w-2xl mx-auto">
            Click any report below to read the full analysis. New reports are added regularly.
          </p>
        </div>

        <div className="grid gap-6 md:grid-cols-2">
          <PredictionSection
            title="Geo-Political Predictions"
            description="Global events, conflicts & diplomacy"
            reports={geoPolitical}
          />
          <PredictionSection
            title="Country / Economy Predictions"
            description="National outlooks & economic forecasts"
            reports={countryEconomy}
          />
          <PredictionSection
            title="Stock Market & Commodity Predictions"
            description="Equity, bullion & commodity trends"
            reports={stockMarket}
          />
          <PredictionSection
            title="Other Mundane Predictions"
            description="Weather, disasters & world events"
            reports={otherMundane}
          />
        </div>

        <AdvertisementBanner />
      </main>
      <SiteFooter />
    </div>
  );
}

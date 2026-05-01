import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Routes, Route, Link, useLocation } from "react-router-dom";
import "./styles.css";

import { SiteFooter } from "@/components/SiteFooter";
import { PredictionSection, type Report } from "@/components/PredictionSection";
import { AdvertisementBanner } from "@/components/AdvertisementBanner";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Mail, Phone, Check } from "lucide-react";

// ---------- Header (React Router version) ----------
const navItems = [
  { to: "/", label: "Home" },
  { to: "/about", label: "About Us" },
  { to: "/subscriptions", label: "Subscription Plans" },
  { to: "/contact", label: "Contact Us" },
] as const;

function SiteHeader() {
  const { pathname } = useLocation();
  return (
    <header className="border-b border-border bg-[var(--gradient-cosmos)] text-primary-foreground">
      <div className="mx-auto max-w-6xl px-6 py-8 text-center">
        <p className="text-xs uppercase tracking-[0.4em] text-[var(--gold)]">Vedic Mundane Astrology</p>
        <h1 className="mt-2 text-4xl md:text-5xl font-semibold text-[var(--gold)]">
          Banthiya Astro Predictions
        </h1>
      </div>
      <nav className="border-t border-white/10 bg-black/20">
        <ul className="mx-auto flex max-w-6xl flex-wrap items-center justify-center gap-2 px-6 py-3 text-sm">
          {navItems.map((item) => {
            const active = pathname === item.to;
            return (
              <li key={item.to}>
                <Link
                  to={item.to}
                  className={`px-4 py-2 uppercase tracking-widest transition-colors ${
                    active ? "text-[var(--gold)]" : "text-primary-foreground/80 hover:text-[var(--gold)]"
                  }`}
                >
                  {item.label}
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>
    </header>
  );
}

// ---------- Pages ----------
const sample: Report[] = [
  { title: "Sample Report 1", url: "#" },
  { title: "Sample Report 2", url: "#" },
  { title: "Sample Report 3", url: "#" },
  { title: "Sample Report 4", url: "#" },
];

const geoPoliticalReports: Report[] = [
  { title: "US–Iran War Prediction — Daywise & Month-wise (Apr–Dec 2026)", url: "/reports/US_IRAN_War_Prediction_Daywise_Month_wise.html", date: "2026" },
  { title: "Predictions Scorecard — Day-wise (14th – 22nd Apr)", url: "/reports/Predictions_Scorecard_Day_Wise_14th_-_22nd_Apr.html", date: "April 2026" },
];

const countryEconomyReports: Report[] = [
  { title: "India — Mundane Astrology 2026", url: "/reports/India_Mundane_Astrology_2026.pdf", date: "2026" },
  { title: "Japan — Mundane Astrology 2026", url: "/reports/Japan_Mundane_Astrology_2026.pdf", date: "2026" },
  { title: "USA — Mundane Astrology 2026", url: "/reports/USA_Mundane_Astrology_2026.pdf", date: "2026" },
];

const stockMarketReports: Report[] = [
  { title: "India Banking Intelligence Report — April 2026", url: "/reports/India_Banking_Intelligence_Report_April2026.pdf", date: "April 2026" },
  { title: "Fertilizer Intelligence Report", url: "/reports/Fertilizer_Intelligence_Report.html", date: "2026" },
];

function HomePage() {
  return (
    <main className="flex-1 mx-auto w-full max-w-6xl px-6 py-12">
      <div className="text-center mb-10">
        <h2 className="text-3xl md:text-4xl font-semibold">Latest Predictions & Reports</h2>
        <p className="mt-3 text-muted-foreground max-w-2xl mx-auto">
          Click any report below to read the full analysis. New reports are added regularly.
        </p>
      </div>
      <div className="grid gap-6 md:grid-cols-2">
        <PredictionSection title="Geo-Political Predictions" description="Global events, conflicts & diplomacy" reports={geoPoliticalReports} />
        <PredictionSection title="Country / Economy Predictions" description="National outlooks & economic forecasts" reports={countryEconomyReports} />
        <PredictionSection title="Stock Market & Commodity Predictions" description="Equity, bullion & commodity trends" reports={stockMarketReports} />
        <PredictionSection title="Other Mundane Predictions" description="Weather, disasters & world events" reports={sample} />
      </div>
      <AdvertisementBanner />
    </main>
  );
}

function AboutPage() {
  return (
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
  );
}

const plans = [
  { name: "Monthly", price: "₹499", period: "/ month", features: ["All 4 categories", "New reports weekly", "Email alerts"] },
  { name: "Quarterly", price: "₹1,299", period: "/ 3 months", features: ["Everything in Monthly", "Priority report access", "Save 13%"], featured: true },
  { name: "Annual", price: "₹4,499", period: "/ year", features: ["Everything in Quarterly", "Exclusive yearly forecast", "Save 25%"] },
];

function SubscriptionsPage() {
  return (
    <main className="flex-1 mx-auto w-full max-w-6xl px-6 py-16">
      <div className="text-center">
        <h2 className="text-4xl font-semibold">Subscription Plans</h2>
        <p className="mt-3 text-muted-foreground">Choose a plan that fits your needs.</p>
      </div>
      <div className="mt-10 grid gap-6 md:grid-cols-3">
        {plans.map((p) => (
          <Card key={p.name} className={`rounded-3xl ${p.featured ? "border-[var(--gold)] border-2 shadow-[var(--shadow-celestial)]" : ""}`}>
            <CardHeader className="text-center">
              <CardTitle className="text-2xl">{p.name}</CardTitle>
              <div className="mt-2">
                <span className="text-4xl font-semibold">{p.price}</span>
                <span className="text-muted-foreground">{p.period}</span>
              </div>
            </CardHeader>
            <CardContent>
              <ul className="space-y-3">
                {p.features.map((f) => (
                  <li key={f} className="flex items-center gap-2 text-sm">
                    <Check className="h-4 w-4 text-[var(--gold)]" /> {f}
                  </li>
                ))}
              </ul>
            </CardContent>
          </Card>
        ))}
      </div>
      <p className="mt-8 text-center text-sm text-muted-foreground">
        Payment integration coming soon. Contact us to subscribe today.
      </p>
    </main>
  );
}

function ContactPage() {
  return (
    <main className="flex-1 mx-auto w-full max-w-3xl px-6 py-16">
      <h2 className="text-4xl font-semibold text-center">Contact Us</h2>
      <p className="mt-3 text-center text-muted-foreground">
        We'd love to hear from you. Reach out using the details below.
      </p>
      <div className="mt-10 grid gap-4 sm:grid-cols-2">
        <a href="mailto:contact@banthiyaastro.com" className="flex items-center gap-3 rounded-2xl border-2 border-border p-6 hover:border-[var(--gold)] transition-colors">
          <Mail className="h-6 w-6 text-[var(--gold)]" />
          <div>
            <p className="text-xs uppercase tracking-widest text-muted-foreground">Email</p>
            <p className="font-medium">contact@banthiyaastro.com</p>
          </div>
        </a>
        <a href="tel:+910000000000" className="flex items-center gap-3 rounded-2xl border-2 border-border p-6 hover:border-[var(--gold)] transition-colors">
          <Phone className="h-6 w-6 text-[var(--gold)]" />
          <div>
            <p className="text-xs uppercase tracking-widest text-muted-foreground">Phone</p>
            <p className="font-medium">+91 00000 00000</p>
          </div>
        </a>
      </div>
    </main>
  );
}

function NotFoundPage() {
  return (
    <main className="flex-1 flex items-center justify-center px-4 py-24">
      <div className="text-center">
        <h1 className="text-7xl font-bold">404</h1>
        <h2 className="mt-4 text-xl font-semibold">Page not found</h2>
        <Link to="/" className="mt-6 inline-flex items-center justify-center rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90">
          Go home
        </Link>
      </div>
    </main>
  );
}

function App() {
  return (
    <div className="min-h-screen flex flex-col bg-background">
      <SiteHeader />
      <Routes>
        <Route path="/" element={<HomePage />} />
        <Route path="/about" element={<AboutPage />} />
        <Route path="/subscriptions" element={<SubscriptionsPage />} />
        <Route path="/contact" element={<ContactPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Routes>
      <SiteFooter />
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>
);

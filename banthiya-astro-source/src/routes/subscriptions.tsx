import { createFileRoute } from "@tanstack/react-router";
import { SiteHeader } from "@/components/SiteHeader";
import { SiteFooter } from "@/components/SiteFooter";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Check } from "lucide-react";

export const Route = createFileRoute("/subscriptions")({
  head: () => ({
    meta: [
      { title: "Subscription Plans — Banthiya Astro Predictions" },
      { name: "description", content: "Subscribe for full access to mundane astrology reports and forecasts." },
      { property: "og:title", content: "Subscription Plans — Banthiya Astro Predictions" },
      { property: "og:description", content: "Plans for accessing premium predictions and reports." },
    ],
  }),
  component: Subscriptions,
});

const plans = [
  { name: "Monthly", price: "₹499", period: "/ month", features: ["All 4 categories", "New reports weekly", "Email alerts"] },
  { name: "Quarterly", price: "₹1,299", period: "/ 3 months", features: ["Everything in Monthly", "Priority report access", "Save 13%"], featured: true },
  { name: "Annual", price: "₹4,499", period: "/ year", features: ["Everything in Quarterly", "Exclusive yearly forecast", "Save 25%"] },
];

function Subscriptions() {
  return (
    <div className="min-h-screen flex flex-col bg-background">
      <SiteHeader />
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
      <SiteFooter />
    </div>
  );
}

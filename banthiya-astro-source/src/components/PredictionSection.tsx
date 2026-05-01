import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { FileText, ExternalLink } from "lucide-react";

export type Report = {
  title: string;
  url: string;
  date?: string;
};

type Props = {
  title: string;
  description?: string;
  reports: Report[];
};

export function PredictionSection({ title, description, reports }: Props) {
  return (
    <Card className="h-full border-2 border-border/60 rounded-3xl shadow-[var(--shadow-card)] hover:border-[var(--gold)] transition-colors">
      <CardHeader className="text-center border-b border-border/50 bg-[var(--gold-soft)]/40 rounded-t-3xl">
        <CardTitle className="text-2xl">{title}</CardTitle>
        {description && (
          <p className="text-sm text-muted-foreground mt-1">{description}</p>
        )}
      </CardHeader>
      <CardContent className="p-6">
        {reports.length === 0 ? (
          <p className="text-sm text-muted-foreground italic text-center py-6">
            Reports coming soon.
          </p>
        ) : (
          <ol className="space-y-3">
            {reports.map((r, i) => (
              <li key={i}>
                <a
                  href={r.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="group flex items-start gap-3 rounded-xl border border-border/60 bg-card p-3 hover:border-[var(--gold)] hover:bg-[var(--gold-soft)]/30 transition-all"
                >
                  <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary text-primary-foreground text-xs font-semibold">
                    {i + 1}
                  </span>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <FileText className="h-4 w-4 text-[var(--gold)] shrink-0" />
                      <span className="font-medium truncate">{r.title}</span>
                    </div>
                    {r.date && (
                      <p className="text-xs text-muted-foreground mt-0.5">{r.date}</p>
                    )}
                  </div>
                  <ExternalLink className="h-4 w-4 text-muted-foreground opacity-0 group-hover:opacity-100 transition-opacity shrink-0 mt-1" />
                </a>
              </li>
            ))}
          </ol>
        )}
      </CardContent>
    </Card>
  );
}

import SwiftUI

struct EODReportView: View {
    @State private var report: EODReport? = nil
    @State private var isLoading = false
    @State private var isRunning = false
    @State private var errorMessage: String? = nil

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                if isLoading {
                    HStack { Spacer(); ProgressView("Loading report..."); Spacer() }
                        .padding(.top, 60)
                } else if let r = report, r.available, let analysis = r.analysis, let stats = r.stats {
                    reportContent(report: r, analysis: analysis, stats: stats)
                } else if let r = report, !r.available {
                    emptyState(message: r.message ?? "No report yet.")
                } else if let err = errorMessage {
                    emptyState(message: err)
                } else {
                    emptyState(message: "No EOD report yet — runs automatically at market close (4 PM ET).")
                }
            }
            .padding()
        }
        .navigationTitle("Daily Report")
        .navigationBarTitleDisplayMode(.large)
        .toolbar {
            ToolbarItem(placement: .navigationBarTrailing) {
                Button {
                    triggerManual()
                } label: {
                    if isRunning {
                        ProgressView().tint(.white)
                    } else {
                        Label("Run Now", systemImage: "arrow.clockwise")
                    }
                }
                .disabled(isRunning)
            }
        }
        .task { await loadReport() }
    }

    // MARK: — Main report

    @ViewBuilder
    private func reportContent(report: EODReport, analysis: EODAnalysis, stats: EODStats) -> some View {
        // Header card
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(report.date ?? "Today")
                    .font(.caption)
                    .foregroundColor(.secondary)
                Spacer()
                gradeLabel(analysis.performanceGrade)
            }
            Text(analysis.headline)
                .font(.headline)
                .fixedSize(horizontal: false, vertical: true)

            Divider()

            HStack(spacing: 24) {
                statCell(title: "Day P&L", value: String(format: "%+.2f%%", stats.dayPlPct),
                         color: stats.dayPlPct >= 0 ? .green : .red)
                statCell(title: "Trades", value: "\(stats.tradesExecuted)")
                statCell(title: "Closed", value: "\(stats.positionsClosed)")
                statCell(title: "Rejected", value: "\(stats.entriesRejected)")
                statCell(title: "Bot Score", value: "\(analysis.botRating)/10")
            }
        }
        .padding()
        .background(Color(.secondarySystemBackground))
        .cornerRadius(14)

        // Key insight
        sectionCard(title: "Key Insight", icon: "lightbulb.fill", color: .yellow) {
            Text(analysis.keyInsight)
                .font(.subheadline)
                .foregroundColor(.primary)
                .fixedSize(horizontal: false, vertical: true)
        }

        // What worked / didn't
        HStack(alignment: .top, spacing: 12) {
            bulletCard(title: "Worked", icon: "checkmark.circle.fill", color: .green,
                       items: analysis.whatWorked)
            bulletCard(title: "Missed", icon: "xmark.circle.fill", color: .red,
                       items: analysis.whatDidnt)
        }

        // Tomorrow's watchlist
        if !analysis.tomorrowWatchlist.isEmpty {
            sectionCard(title: "Tomorrow's Watchlist", icon: "eye.fill", color: .blue) {
                VStack(spacing: 10) {
                    ForEach(analysis.tomorrowWatchlist) { item in
                        HStack(alignment: .top, spacing: 10) {
                            Text(item.action.uppercased())
                                .font(.caption2.bold())
                                .padding(.horizontal, 6)
                                .padding(.vertical, 3)
                                .background(actionColor(item.action).opacity(0.15))
                                .foregroundColor(actionColor(item.action))
                                .cornerRadius(6)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(item.symbol).font(.subheadline.bold())
                                Text(item.thesis)
                                    .font(.caption)
                                    .foregroundColor(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                        if item.id != analysis.tomorrowWatchlist.last?.id {
                            Divider()
                        }
                    }
                }
            }
        }

        // Risk note
        if !analysis.riskNote.isEmpty {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundColor(.orange)
                    .font(.subheadline)
                Text(analysis.riskNote)
                    .font(.subheadline)
                    .foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding()
            .background(Color.orange.opacity(0.08))
            .cornerRadius(12)
        }

        if let gen = report.generatedAt {
            Text("Generated \(friendlyTime(gen))")
                .font(.caption2)
                .foregroundColor(.secondary)
                .frame(maxWidth: .infinity, alignment: .center)
        }
    }

    // MARK: — Sub-components

    private func statCell(title: String, value: String, color: Color = .primary) -> some View {
        VStack(spacing: 2) {
            Text(value)
                .font(.subheadline.bold())
                .foregroundColor(color)
            Text(title)
                .font(.caption2)
                .foregroundColor(.secondary)
        }
    }

    private func gradeLabel(_ grade: String) -> some View {
        Text(grade)
            .font(.title2.bold())
            .foregroundColor(gradeColor(grade))
            .frame(width: 36, height: 36)
            .background(gradeColor(grade).opacity(0.15))
            .cornerRadius(8)
    }

    private func sectionCard<Content: View>(title: String, icon: String, color: Color, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(title, systemImage: icon)
                .font(.subheadline.bold())
                .foregroundColor(color)
            content()
        }
        .padding()
        .background(Color(.secondarySystemBackground))
        .cornerRadius(14)
    }

    private func bulletCard(title: String, icon: String, color: Color, items: [String]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(title, systemImage: icon)
                .font(.caption.bold())
                .foregroundColor(color)
            if items.isEmpty {
                Text("—").font(.caption).foregroundColor(.secondary)
            } else {
                ForEach(items, id: \.self) { item in
                    Text("• \(item)")
                        .font(.caption)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding()
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(.secondarySystemBackground))
        .cornerRadius(14)
    }

    private func emptyState(message: String) -> some View {
        VStack(spacing: 16) {
            Image(systemName: "chart.bar.doc.horizontal")
                .font(.system(size: 48))
                .foregroundColor(.secondary)
            Text("Daily Report")
                .font(.headline)
            Text(message)
                .font(.subheadline)
                .foregroundColor(.secondary)
                .multilineTextAlignment(.center)
            Button("Run Analysis Now") { triggerManual() }
                .buttonStyle(.borderedProminent)
                .disabled(isRunning)
        }
        .padding(.top, 60)
        .frame(maxWidth: .infinity)
    }

    // MARK: — Helpers

    private func gradeColor(_ grade: String) -> Color {
        switch grade {
        case "A": return .green
        case "B": return Color(red: 0.4, green: 0.8, blue: 0.4)
        case "C": return .yellow
        case "D": return .orange
        case "F": return .red
        default:  return .secondary
        }
    }

    private func actionColor(_ action: String) -> Color {
        switch action.lowercased() {
        case "buy":   return .green
        case "short": return .red
        default:      return .blue
        }
    }

    private func friendlyTime(_ iso: String) -> String {
        let df = ISO8601DateFormatter()
        df.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = df.date(from: iso) {
            let f = DateFormatter()
            f.dateStyle = .short
            f.timeStyle = .short
            return f.string(from: d)
        }
        return iso
    }

    // MARK: — Network

    private func loadReport() async {
        isLoading = true
        defer { isLoading = false }
        do {
            report = try await APIService.shared.getEODReport()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func triggerManual() {
        isRunning = true
        Task {
            do {
                try await APIService.shared.triggerEODAnalysis()
                try? await Task.sleep(nanoseconds: 15_000_000_000) // wait 15s for Claude
                report = try await APIService.shared.getEODReport()
            } catch {
                errorMessage = error.localizedDescription
            }
            isRunning = false
        }
    }
}

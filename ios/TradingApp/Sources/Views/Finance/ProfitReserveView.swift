import SwiftUI

struct ProfitReserveView: View {
    @State private var reserve: ReserveStatus? = nil
    @State private var isLoading = true
    @State private var error: String? = nil
    @State private var showResetConfirm = false
    @State private var isResetting = false
    @State private var resetSuccess: String? = nil

    // Local copy of reserve % for editing (synced via risk settings)
    @State private var reservePct: Double = 0.0
    @State private var isSavingPct = false

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                if isLoading {
                    ProgressView("Loading reserve…").padding(.top, 40)
                } else if let err = error {
                    Text(err).foregroundStyle(.red).padding()
                } else if let r = reserve {

                    // ── Reserve balance card ──
                    VStack(spacing: 16) {
                        HStack {
                            VStack(alignment: .leading, spacing: 4) {
                                Text("Reserved Profits")
                                    .font(.subheadline)
                                    .foregroundStyle(.secondary)
                                Text("$\(String(format: "%.2f", r.reservedCash))")
                                    .font(.system(size: 36, weight: .bold, design: .rounded))
                                    .foregroundStyle(r.reservedCash > 0 ? .green : .primary)
                            }
                            Spacer()
                            ZStack {
                                Circle()
                                    .fill(Color.green.opacity(0.12))
                                    .frame(width: 56, height: 56)
                                Image(systemName: "banknote.fill")
                                    .foregroundStyle(.green)
                                    .font(.system(size: 24))
                            }
                        }

                        Text(r.message)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, alignment: .leading)

                        if r.reservedCash > 0 {
                            Button {
                                showResetConfirm = true
                            } label: {
                                Label("Withdraw Reserve", systemImage: "arrow.down.circle")
                                    .frame(maxWidth: .infinity)
                            }
                            .buttonStyle(.borderedProminent)
                            .tint(.green)
                            .disabled(isResetting)
                        }

                        if let msg = resetSuccess {
                            Text(msg)
                                .font(.caption)
                                .foregroundStyle(.green)
                                .transition(.opacity)
                        }
                    }
                    .padding()
                    .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))

                    // ── Reserve % setting ──
                    VStack(alignment: .leading, spacing: 12) {
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Reserve Rate")
                                    .font(.headline)
                                Text("% of each profitable trade to set aside")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                            Text("\(Int(reservePct))%")
                                .font(.title2).fontWeight(.bold)
                                .foregroundStyle(.blue)
                                .monospacedDigit()
                        }

                        Slider(value: $reservePct, in: 0...50, step: 1) { editing in
                            if !editing { saveReservePct() }
                        }
                        .tint(.blue)

                        HStack {
                            Text("0% = disabled")
                                .font(.caption2).foregroundStyle(.tertiary)
                            Spacer()
                            Text("50% max")
                                .font(.caption2).foregroundStyle(.tertiary)
                        }

                        if isSavingPct {
                            HStack(spacing: 6) {
                                ProgressView().scaleEffect(0.7)
                                Text("Saving…").font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                    .padding()
                    .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))

                    // ── How it works card ──
                    VStack(alignment: .leading, spacing: 10) {
                        Text("How it works")
                            .font(.subheadline).fontWeight(.semibold)

                        InfoRow(icon: "arrow.up.right.circle.fill", color: .green,
                                text: "Every time a position closes with a profit, \(Int(reservePct))% of that gain is moved to the reserve.")
                        InfoRow(icon: "lock.fill", color: .blue,
                                text: "The bot never trades with reserved cash — it's protected from future losses.")
                        InfoRow(icon: "arrow.down.circle.fill", color: .orange,
                                text: "Withdraw anytime. The reserve resets to $0 so you can start accumulating again.")
                    }
                    .padding()
                    .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))
                }
            }
            .padding()
        }
        .navigationTitle("Profit Reserve")
        .navigationBarTitleDisplayMode(.large)
        .task { await load() }
        .refreshable { await load() }
        .confirmationDialog(
            "Withdraw \(reserve.map { "$\(String(format: "%.2f", $0.reservedCash))" } ?? "reserve")?",
            isPresented: $showResetConfirm,
            titleVisibility: .visible
        ) {
            Button("Withdraw Full Balance", role: .destructive) {
                Task { await withdraw() }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This marks the reserved amount as withdrawn. The reserve resets to $0. In live trading, transfer this amount from your Alpaca account.")
        }
    }

    private func load() async {
        isLoading = true
        error = nil
        do {
            reserve = try await APIService.shared.getReserve()
            reservePct = reserve?.profitReservePct ?? 0.0
        } catch {
            self.error = "Could not load reserve: \(error.localizedDescription)"
        }
        isLoading = false
    }

    private func saveReservePct() {
        guard !isSavingPct else { return }
        isSavingPct = true
        Task {
            do {
                var settings = try await APIService.shared.getRiskSettings()
                settings.profit_reserve_pct = reservePct
                try await APIService.shared.setRiskSettings(settings)
            } catch {
                await MainActor.run {
                    self.error = "Failed to save reserve rate — \(error.localizedDescription)"
                    // Reset slider to server value so user sees the actual saved rate
                }
                await load()
            }
            await MainActor.run { isSavingPct = false }
        }
    }

    private func withdraw() async {
        isResetting = true
        resetSuccess = nil
        do {
            let result = try await APIService.shared.resetReserve()
            // Reload first so balance updates, then show success message
            await load()
            await MainActor.run { resetSuccess = result.message }
            // Auto-clear success after 3s
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            await MainActor.run { resetSuccess = nil }
        } catch {
            await MainActor.run {
                self.error = "Withdraw failed: \(error.localizedDescription)"
            }
        }
        await MainActor.run { isResetting = false }
    }
}

private struct InfoRow: View {
    let icon: String
    let color: Color
    let text: String

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: icon)
                .foregroundStyle(color)
                .font(.system(size: 16))
                .frame(width: 20)
            Text(text)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }
}

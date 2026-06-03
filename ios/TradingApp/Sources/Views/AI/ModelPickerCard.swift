import SwiftUI

// All available models — same list for primary and secondary so user can freely mix
private let allModels = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-3-pro",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
    "claude-opus-4-6",
]

struct ModelPickerCard: View {
    @State private var isSaving = false
    @State private var saveSuccess = false
    @State private var errorMessage: String? = nil

    // Selections — shown immediately with defaults, updated from API on load
    @State private var criticalPrimary:   String = "gemini-2.5-pro"
    @State private var criticalSecondary: String = "claude-sonnet-4-6"
    @State private var standardPrimary:   String = "gemini-2.5-flash"
    @State private var standardSecondary: String = "claude-haiku-4-5-20251001"

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {

            // ── Header ──────────────────────────────────────────────────
            HStack(spacing: 12) {
                ZStack {
                    Circle().fill(Color.blue.opacity(0.12)).frame(width: 36, height: 36)
                    Image(systemName: "cpu")
                        .foregroundStyle(.blue).font(.system(size: 15))
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text("AI Models")
                        .font(.subheadline).fontWeight(.semibold)
                    Text("Primary + secondary for each tier")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                if isSaving {
                    ProgressView().scaleEffect(0.8)
                } else if saveSuccess {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundStyle(.green)
                        .transition(.scale.combined(with: .opacity))
                }
            }
            .padding()

            Divider().padding(.horizontal)

            // ── Critical tier ────────────────────────────────────────────
            tierSection(
                label: "Critical",
                subtitle: "Step 2 trade decisions only",
                primaryOptions: allModels,
                secondaryOptions: allModels,
                primary: $criticalPrimary,
                secondary: $criticalSecondary,
                recommendedPrimary: "claude-sonnet-4-6",
                recommendedSecondary: "gemini-2.5-flash",
                color: .red
            )

            Divider().padding(.horizontal)

            // ── Non-Critical tier ────────────────────────────────────────
            tierSection(
                label: "Non-Critical",
                subtitle: "Step 1 scan · Earnings · EOD · Daily picks · Predictions · Suggestions",
                primaryOptions: allModels,
                secondaryOptions: allModels,
                primary: $standardPrimary,
                secondary: $standardSecondary,
                recommendedPrimary: "claude-haiku-4-5-20251001",
                recommendedSecondary: "gemini-2.5-flash",
                color: .orange
            )

            if let err = errorMessage {
                Text(err).font(.caption).foregroundStyle(.red)
                    .padding(.horizontal).padding(.bottom, 4)
            }

            Divider().padding(.horizontal)

            Button(action: saveSettings) {
                Label("Save Model Settings", systemImage: "checkmark.circle")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .padding()
            .disabled(isSaving)
        }
        .background(Color(.systemGray6))
        .cornerRadius(16)
        .task { await loadSettings() }
    }

    // ── Tier section ────────────────────────────────────────────────────
    @ViewBuilder
    private func tierSection(
        label: String,
        subtitle: String,
        primaryOptions: [String],
        secondaryOptions: [String],
        primary: Binding<String>,
        secondary: Binding<String>,
        recommendedPrimary: String,
        recommendedSecondary: String,
        color: Color
    ) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Circle().fill(color).frame(width: 8, height: 8)
                Text(label)
                    .font(.caption).fontWeight(.semibold).foregroundStyle(color)
                Spacer()
                HStack(spacing: 3) {
                    Image(systemName: "star.fill")
                        .font(.system(size: 7))
                        .foregroundStyle(.secondary.opacity(0.7))
                    Text("\(friendlyName(recommendedPrimary)) · \(friendlyName(recommendedSecondary))")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
            Text(subtitle)
                .font(.caption2).foregroundStyle(.secondary)

            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("\(label) Primary").font(.caption2).foregroundStyle(.secondary)
                    Picker("\(label) Primary", selection: primary) {
                        ForEach(primaryOptions, id: \.self) { model in
                            Text(displayName(model, recommended: recommendedPrimary)).tag(model)
                        }
                    }
                    .pickerStyle(.menu)
                    .labelsHidden()
                    .font(.caption)
                }
                .frame(maxWidth: .infinity, alignment: .leading)

                VStack(alignment: .leading, spacing: 4) {
                    Text("\(label) Secondary").font(.caption2).foregroundStyle(.secondary)
                    Picker("\(label) Secondary", selection: secondary) {
                        ForEach(secondaryOptions, id: \.self) { model in
                            Text(displayName(model, recommended: recommendedSecondary)).tag(model)
                        }
                    }
                    .pickerStyle(.menu)
                    .labelsHidden()
                    .font(.caption)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding()
    }

    // ── Helpers ─────────────────────────────────────────────────────────
    private func friendlyName(_ model: String) -> String {
        switch model {
        case "gemini-2.5-pro":            return "Gemini 2.5 Pro"
        case "gemini-2.5-flash":          return "Gemini 2.5 Flash"
        case "gemini-3-pro":              return "Gemini 3 Pro"
        case "claude-sonnet-4-6":         return "Sonnet 4.6"
        case "claude-haiku-4-5-20251001": return "Haiku 4.5"
        case "claude-opus-4-6":           return "Opus 4.6"
        default:                           return model
        }
    }

    private func displayName(_ model: String, recommended: String) -> String {
        friendlyName(model)
    }

    private func loadSettings() async {
        if let loaded = try? await APIService.shared.getModelSettings() {
            criticalPrimary   = loaded.pro.primary
            criticalSecondary = loaded.pro.fallback
            standardPrimary   = loaded.standard.primary
            standardSecondary = loaded.standard.fallback
        }
        // If API fails, defaults already set in @State — pickers still usable
    }

    private func saveSettings() {
        isSaving = true
        saveSuccess = false
        errorMessage = nil
        Task {
            do {
                let update = ModelSettingsUpdate(
                    pro_primary:       criticalPrimary,
                    pro_fallback:      criticalSecondary,
                    standard_primary:  standardPrimary,
                    standard_fallback: standardSecondary
                )
                try await APIService.shared.updateModelSettings(update)
                await MainActor.run {
                    isSaving = false
                    saveSuccess = true
                }
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                await MainActor.run { saveSuccess = false }
            } catch {
                await MainActor.run {
                    isSaving = false
                    errorMessage = "Save failed: \(error.localizedDescription)"
                }
            }
        }
    }
}

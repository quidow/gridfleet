import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist', 'test-results', 'playwright-report', 'src/api/openapi.ts']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    rules: {
      'no-restricted-syntax': [
        'error',
        {
          selector: "JSXOpeningElement[name.name='select']",
          message: 'Use <Select> from components/ui instead of raw <select>.',
        },
        {
          selector:
            "JSXAttribute[name.name='className'] Literal[value=/^(?=.*\\bbg-surface-1\\b)(?=.*\\brounded-lg\\b)(?=.*\\bborder\\b)(?=.*\\bborder-border\\b).*$/]",
          message: 'Use <Card> or .card utility - never raw card class string.',
        },
        {
          selector:
            "JSXAttribute[name.name='className'] Literal[value=/^(?=.*\\brounded-md\\b)(?=.*\\bborder-border-strong\\b)(?=.*\\bfocus:ring-accent\\b)(?=.*\\btext-sm\\b)(?=.*\\bpx-3\\b)(?=.*\\bpy-2\\b).*$/]",
          message: 'Use <TextField>/<Textarea> primitives instead of hand-rolled input classes.',
        },
        {
          selector: "JSXOpeningElement[name.name='input']:has(JSXAttribute[name.name='type'][value.value='date'])",
          message: 'Use <DateInput> from components/ui instead of raw <input type=date>.',
        },
      ],
    },
  },
  {
    files: [
      'src/components/RowActionsMenu.tsx',
      'src/components/SettingField.tsx',
      'src/components/SettingsSection.tsx',
      'src/components/analytics/FleetCapacityTab.tsx',
      'src/components/dashboard/OperationsSection.tsx',
      'src/components/deviceDetail/DeviceLogsEmptyPanel.tsx',
      'src/components/deviceDetail/DeviceLogsPanel.tsx',
      'src/components/deviceDetail/DeviceSessionOutcomeHeatmapPanel.tsx',
      'src/components/deviceDetail/DeviceStatStrip.tsx',
      'src/components/deviceDetail/StateHistoryPanel.tsx',
      'src/components/hostDetail/HostDevicesPanel.tsx',
      'src/components/hostDetail/HostDiagnosticsPanel.tsx',
      'src/components/hostDetail/HostDriversPanel.tsx',
      'src/components/hostDetail/HostOverviewPanel.tsx',
      'src/components/hostDetail/HostOverviewResourceStrip.tsx',
      'src/components/hostDetail/HostPluginsPanel.tsx',
      'src/components/hostDetail/HostResourceTelemetryPanel.tsx',
      'src/components/hostDetail/HostTerminalPanel.tsx',
      'src/components/hostDetail/HostToolVersionsPanel.tsx',
      'src/components/settings/WebhookRegistryPanel.tsx',
      'src/components/ui/Card.tsx',
      'src/components/ui/CursorPagination.tsx',
      'src/components/ui/DataTable.tsx',
      'src/components/ui/DateInput.tsx',
      'src/components/ui/Pagination.tsx',
      'src/components/ui/Popover.tsx',
      'src/components/ui/Select.tsx',
      'src/pages/DeviceDetail.tsx',
      'src/pages/Devices.tsx',
      'src/pages/Login.tsx',
      'src/pages/devices/DevicesFiltersBar.tsx',
      'src/pages/devices/FilterBuilder.tsx',
    ],
    rules: {
      'no-restricted-syntax': 'off',
    },
  },
])

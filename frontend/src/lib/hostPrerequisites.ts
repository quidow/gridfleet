const PREREQUISITE_DESCRIPTIONS: Record<string, string> = {
  adb: 'Required for Android mobile, Android TV, and Fire TV devices.',
  appium: 'Legacy host-global Appium runtime. Current agents install Appium per driver pack.',
  go_ios: 'Required for iOS real-device battery telemetry.',
  java: 'Required for Android driver build tools.',
  xcodebuild: 'Required for iOS and tvOS automation on macOS.',
};

export function describeHostPrerequisite(name: string) {
  return PREREQUISITE_DESCRIPTIONS[name] ?? 'Required for one or more host capabilities.';
}

export function formatHostPrerequisiteList(items: string[]) {
  return items.length ? items.join(', ') : 'All prerequisites detected';
}

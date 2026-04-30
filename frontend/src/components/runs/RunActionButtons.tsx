import Button from '../ui/Button';

type Props = {
  onCancel: () => void;
  onForceRelease: () => void;
  size?: 'sm' | 'md';
};

export default function RunActionButtons({ onCancel, onForceRelease, size = 'sm' }: Props) {
  return (
    <div className="flex items-center gap-2">
      <Button variant="secondary" size={size} onClick={onCancel}>
        Cancel
      </Button>
      <Button variant="danger" size={size} onClick={onForceRelease}>
        Force Release
      </Button>
    </div>
  );
}

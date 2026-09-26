"""Inspect receipts and record externally verified text outcomes; never sends."""
import argparse
import json

from .delivery_store import DeliveryBusy, DeliveryStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--media-dir', required=True)
    parser.add_argument('--notification-id', required=True)
    parser.add_argument('--step')
    parser.add_argument('--outcome', choices=['delivered', 'not-delivered'])
    parser.add_argument('--evidence')
    args = parser.parse_args()
    if any((args.step, args.outcome, args.evidence)) and not all((args.step, args.outcome, args.evidence)):
        parser.error('resolution requires --step, --outcome and --evidence')
    store = DeliveryStore(args.media_dir)
    try:
        if args.step:
            store.resolve(args.notification_id, args.step, outcome=args.outcome, evidence=args.evidence)
        print(json.dumps(store.steps(args.notification_id), ensure_ascii=False))
    except (DeliveryBusy, ValueError) as error:
        parser.error(str(error) or 'notification is being delivered')
    finally:
        store.close()


if __name__ == '__main__':
    main()

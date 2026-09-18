/* AUTODEDUP · one advert, whole — the row every review DIALOG is made of.
 *
 * THE DIALOG SHOWS THE ADVERT'S OWN TEXT, because the photos and the attribute
 * row are exactly what a developer project makes identical: five units in one
 * building share the render set, the price band, the disposition and the storey
 * count, and differ only in what the ad SAYS — "byt č. 14", "ve 4. NP",
 * "orientace na jih", "68 m²". So the text sits under the facts and over the
 * controls, because it is read last and decides.
 *
 * The carousel shows ALL the photos here (the queue card ships the first 12),
 * which is why the drawer renders the carousel and the attributes rather than
 * the single-cover card the queue uses.
 */

import { Link } from 'react-router-dom';

import type { AutodedupMemberDetail } from '@/lib/api';
import ImageCarousel from '@/components/ImageCarousel';
import { imageSrc } from '@/lib/imageUrl';
import {
  MissingPhotoTile,
  memberAttrs,
  memberListingPath,
} from '@/components/autodedup/ListingMini';
import MemberText from '@/components/autodedup/MemberText';
import { UnitSelect, type SplitControls } from '@/components/autodedup/UnitSplit';

export default function MemberRow({
  member,
  split,
  count,
  /* What the letter control says and whether it can be moved. The candidate
   * dialog locks the adverts of an already-merged group to one letter — moving
   * one of them is a statement about THAT group, and the Groups page is where a
   * group is split. */
  unitLabel,
  unitDisabled = false,
  /* A line above the attributes saying this advert is already merged, and into
   * what: on the candidate surface a locked advert looks exactly like a free one
   * otherwise, and the difference is the whole reason its letter will not move. */
  badge,
}: {
  member: AutodedupMemberDetail;
  split: SplitControls;
  count: number;
  unitLabel?: string;
  unitDisabled?: boolean;
  badge?: string;
}) {
  /* The carousel wants render-ready urls; these photos carry no CLIP tag on
   * this surface, so both decorations are explicitly null rather than faked. */
  const images = member.images.map((img) => ({
    url: imageSrc(img),
    tag: null,
    confidence: null,
    renderScore: null,
  }));
  const inApp = memberListingPath(member);
  return (
    <li className="grid gap-3 sm:grid-cols-[18rem_1fr] items-start">
      <ImageCarousel
        images={images}
        aspect="aspect-[4/3]"
        /* A frame the portal refuses says so, here too: the dialog is where the
         * operator looks hardest at the photos. */
        fallback={<MissingPhotoTile source={member.source} reason="foto nedostupné" />}
      />
      <div className="space-y-1">
        <p className="text-[0.75rem] text-[var(--color-ink-2)]">
          <span className="font-mono">#{member.listing_id}</span> · {member.source} ·{' '}
          {member.is_active ? 'aktivní' : 'staženo'}
        </p>
        {badge && (
          <p className="text-[0.66rem] text-[var(--color-ink-3)]">{badge}</p>
        )}
        <dl className="grid grid-cols-2 gap-x-3 gap-y-0.5 text-[0.72rem] max-w-[22rem]">
          {memberAttrs(member).map(([label, value]) => (
            <div key={label} className="flex items-baseline justify-between gap-2">
              <dt className="text-[var(--color-ink-4)]">{label}</dt>
              <dd className="font-mono tabular-nums text-[var(--color-ink-2)]">{value}</dd>
            </div>
          ))}
        </dl>
        <MemberText
          title={member.title}
          text={member.description}
          label={`#${member.listing_id}`}
        />
        <UnitSelect
          listingId={member.listing_id}
          units={split.state.units}
          count={count}
          label={unitLabel}
          disabled={unitDisabled}
          onChange={(unit) => split.setUnit(member.listing_id, unit)}
        />
        <p className="flex items-center gap-3 text-[0.7rem]">
          {inApp && (
            <Link
              to={inApp}
              className="text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2"
            >
              Detail
            </Link>
          )}
          {member.source_url && (
            <a
              href={member.source_url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2"
            >
              Na portálu
            </a>
          )}
        </p>
      </div>
    </li>
  );
}

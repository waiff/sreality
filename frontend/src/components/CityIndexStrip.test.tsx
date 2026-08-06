import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import CityIndexStrip from './CityIndexStrip';
import { buildCityQualityByObec } from '@/lib/useCityQuality';
import type { CityIndexDefinition, CityIndexValue, CuratedCity } from '@/lib/queries';

const def = (index_name: string, over: Partial<CityIndexDefinition> = {}): CityIndexDefinition => ({
  index_name,
  label_cs: `Index ${index_name}`,
  label_en: null,
  category: 'sub_index',
  scale_min: 0,
  scale_max: 10,
  higher_is_better: true,
  sort_order: 0,
  ...over,
} as CityIndexDefinition);

const DEFS = [
  def('celkove_hodnoceni', { category: 'overall', label_cs: 'Celkové hodnocení' }),
  def('prirustek_obyvatel'),
  def('stehovani_mladych'),
  def('pracovni_mista'),
];

const CITY: CuratedCity = {
  city_id: 5,
  name: 'Beroun',
  kraj_name: 'Středočeský kraj',
  lat: 49.9,
  lng: 14.07,
  default_radius_m: 5000,
  population: 19_000,
  population_as_of_year: 2024,
  admin_boundary_id: 531057,
};

const values = (vals: Record<string, number>): CityIndexValue[] =>
  Object.entries(vals).map(([index_name, value]) => ({
    city_id: 5,
    index_name,
    value,
  }));

const quality = (vals: Record<string, number>) =>
  buildCityQualityByObec([CITY], DEFS, values(vals)).get(531057);

describe('buildCityQualityByObec', () => {
  /* The join key. All 206 curated cities carry an admin_boundary_id, so
   * properties.obec_id = curated_cities.admin_boundary_id reproduces the SQL
   * predicate's ST_Covers arm without PostGIS. */
  it('keys the lookup on admin_boundary_id, not city_id', () => {
    const map = buildCityQualityByObec([CITY], DEFS, values({ celkove_hodnoceni: 7 }));
    expect(map.has(531057)).toBe(true);
    expect(map.has(5)).toBe(false);
  });

  it('skips a city with no boundary rather than mis-keying it', () => {
    const map = buildCityQualityByObec(
      [{ ...CITY, admin_boundary_id: null }],
      DEFS,
      values({ celkove_hodnoceni: 7 }),
    );
    expect(map.size).toBe(0);
  });

  it('keeps the four card slugs in their fixed order', () => {
    const q = quality({ celkove_hodnoceni: 7 })!;
    expect(q.readings.map((r) => r.index_name)).toEqual([
      'celkove_hodnoceni',
      'prirustek_obyvatel',
      'stehovani_mladych',
      'pracovni_mista',
    ]);
  });
});

describe('<CityIndexStrip>', () => {
  /* About half of all properties are outside every curated city. An empty strip
   * would read as "this city scores badly"; absence must be absence. */
  it('renders nothing when the property is in no curated city', () => {
    const { container } = render(<CityIndexStrip quality={undefined} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders one fixed cell per index, in order, with the value printed', () => {
    render(
      <CityIndexStrip
        quality={quality({
          celkove_hodnoceni: 7.4,
          prirustek_obyvatel: 4.1,
          stehovani_mladych: 6.8,
          pracovni_mista: 8.2,
        })}
      />,
    );
    for (const abbr of ['CH', 'PO', 'SM', 'PM']) {
      expect(screen.getByText(abbr)).toBeInTheDocument();
    }
    // The printed value is what carries precision — and is the required relief
    // for a sub-3:1 fill, so colour is never the only channel.
    expect(screen.getByText('7,4')).toBeInTheDocument();
    expect(screen.getByText('8,2')).toBeInTheDocument();
  });

  it('em-dashes an index the city has no reading for', () => {
    render(<CityIndexStrip quality={quality({ celkove_hodnoceni: 7.4 })} />);
    expect(screen.getAllByText('—')).toHaveLength(3);
  });

  it('names the city in each cell tooltip', () => {
    render(<CityIndexStrip quality={quality({ celkove_hodnoceni: 9.1 })} />);
    expect(screen.getByLabelText(/Celkové hodnocení: 9,1 \/ 10 — nadprůměr \(Beroun\)/))
      .toBeInTheDocument();
  });
});

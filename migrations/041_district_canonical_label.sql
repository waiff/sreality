-- 041_district_canonical_label.sql
-- The `listings.district` text column was historically derived by
-- slicing the last comma-separated segment of the human-readable
-- locality string (scraper.parser._district). Sreality formats
-- locality inconsistently: listings in the okresní město end up
-- labelled with the city name ("Havlíčkův Brod"), while listings in
-- surrounding villages get the "okres X" label. The two forms never
-- match each other, so PostgREST .in('district', ['okres Havlíčkův Brod'])
-- silently dropped the city-centre listings.
--
-- Fix: canonicalize `district` from the stable integer
-- `locality_district_id` that sreality already sends on every
-- listing (populated by migration 004). 1..77 map to the 76 Czech
-- okresy (minus 47); 47 + 5001..5022 collapse to a single "Praha"
-- label per operator preference. Foreign listings (id = -1) are
-- left untouched so their country-name text continues to surface in
-- the District typeahead.

create table sreality_districts (
    id    integer primary key,
    label text    not null
);

insert into sreality_districts (id, label) values
    (1,  'okres České Budějovice'),
    (2,  'okres Český Krumlov'),
    (3,  'okres Jindřichův Hradec'),
    (4,  'okres Písek'),
    (5,  'okres Prachatice'),
    (6,  'okres Strakonice'),
    (7,  'okres Tábor'),
    (8,  'okres Domažlice'),
    (9,  'okres Cheb'),
    (10, 'okres Karlovy Vary'),
    (11, 'okres Klatovy'),
    (12, 'okres Plzeň-město'),
    (13, 'okres Plzeň-jih'),
    (14, 'okres Plzeň-sever'),
    (15, 'okres Rokycany'),
    (16, 'okres Sokolov'),
    (17, 'okres Tachov'),
    (18, 'okres Česká Lípa'),
    (19, 'okres Děčín'),
    (20, 'okres Chomutov'),
    (21, 'okres Jablonec nad Nisou'),
    (22, 'okres Liberec'),
    (23, 'okres Litoměřice'),
    (24, 'okres Louny'),
    (25, 'okres Most'),
    (26, 'okres Teplice'),
    (27, 'okres Ústí nad Labem'),
    (28, 'okres Hradec Králové'),
    (29, 'okres Chrudim'),
    (30, 'okres Jičín'),
    (31, 'okres Náchod'),
    (32, 'okres Pardubice'),
    (33, 'okres Rychnov nad Kněžnou'),
    (34, 'okres Semily'),
    (35, 'okres Svitavy'),
    (36, 'okres Trutnov'),
    (37, 'okres Ústí nad Orlicí'),
    (38, 'okres Zlín'),
    (39, 'okres Kroměříž'),
    (40, 'okres Prostějov'),
    (41, 'okres Uherské Hradiště'),
    (42, 'okres Olomouc'),
    (43, 'okres Přerov'),
    (44, 'okres Šumperk'),
    (45, 'okres Vsetín'),
    (46, 'okres Jeseník'),
    (47, 'Praha'),
    (48, 'okres Benešov'),
    (49, 'okres Beroun'),
    (50, 'okres Kladno'),
    (51, 'okres Kolín'),
    (52, 'okres Kutná Hora'),
    (53, 'okres Mladá Boleslav'),
    (54, 'okres Mělník'),
    (55, 'okres Nymburk'),
    (56, 'okres Praha-východ'),
    (57, 'okres Praha-západ'),
    (58, 'okres Příbram'),
    (59, 'okres Rakovník'),
    (60, 'okres Bruntál'),
    (61, 'okres Frýdek-Místek'),
    (62, 'okres Karviná'),
    (63, 'okres Nový Jičín'),
    (64, 'okres Opava'),
    (65, 'okres Ostrava-město'),
    (66, 'okres Havlíčkův Brod'),
    (67, 'okres Jihlava'),
    (68, 'okres Pelhřimov'),
    (69, 'okres Třebíč'),
    (70, 'okres Žďár nad Sázavou'),
    (71, 'okres Blansko'),
    (72, 'okres Brno-město'),
    (73, 'okres Brno-venkov'),
    (74, 'okres Břeclav'),
    (75, 'okres Hodonín'),
    (76, 'okres Vyškov'),
    (77, 'okres Znojmo'),
    -- Praha 1..22 collapse to a single "Praha" label per operator
    -- preference. The locality_district_id column still carries the
    -- finer 5001..5022 value if we want sub-district granularity
    -- back in a future widget.
    (5001, 'Praha'),
    (5002, 'Praha'),
    (5003, 'Praha'),
    (5004, 'Praha'),
    (5005, 'Praha'),
    (5006, 'Praha'),
    (5007, 'Praha'),
    (5008, 'Praha'),
    (5009, 'Praha'),
    (5010, 'Praha'),
    (5011, 'Praha'),
    (5012, 'Praha'),
    (5013, 'Praha'),
    (5014, 'Praha'),
    (5015, 'Praha'),
    (5016, 'Praha'),
    (5017, 'Praha'),
    (5018, 'Praha'),
    (5019, 'Praha'),
    (5020, 'Praha'),
    (5021, 'Praha'),
    (5022, 'Praha');

update listings l
   set district = d.label
  from sreality_districts d
 where l.locality_district_id = d.id
   and l.district is distinct from d.label;

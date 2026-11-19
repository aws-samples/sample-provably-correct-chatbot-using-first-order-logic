// Display-only expansion of well-known ontology acronyms into their full
// names, e.g. "GO" -> "Gene Ontology (GO)". This affects LABELS only; the
// stored theory name/id is never changed, so selection and queries are
// unaffected. A name that is not a recognized acronym renders verbatim.

// Keyed by the UPPERCASED acronym so matching is case-insensitive.
const ONTOLOGY_FULL_NAMES: Record<string, string> = {
  GO: 'Gene Ontology',
  CHEBI: 'Chemical Entities of Biological Interest',
  DOID: 'Disease Ontology',
  DO: 'Disease Ontology',
  HPO: 'Human Phenotype Ontology',
  HP: 'Human Phenotype Ontology',
  MONDO: 'Mondo Disease Ontology',
  UBERON: 'Uber-anatomy Ontology',
  PATO: 'Phenotype And Trait Ontology',
  CL: 'Cell Ontology',
  CLO: 'Cell Line Ontology',
  SO: 'Sequence Ontology',
  PO: 'Plant Ontology',
  ENVO: 'Environment Ontology',
  BFO: 'Basic Formal Ontology',
  OBI: 'Ontology for Biomedical Investigations',
  EFO: 'Experimental Factor Ontology',
  NCIT: 'NCI Thesaurus',
  SNOMED: 'SNOMED Clinical Terms',
  'SNOMED CT': 'SNOMED Clinical Terms',
  LOINC: 'Logical Observation Identifiers Names and Codes',
  MESH: 'Medical Subject Headings',
  ICD: 'International Classification of Diseases',
  OMIM: 'Online Mendelian Inheritance in Man',
  RXNORM: 'RxNorm',
  FMA: 'Foundational Model of Anatomy',
  MA: 'Mouse Adult Gross Anatomy Ontology',
  ZFA: 'Zebrafish Anatomy Ontology',
  PR: 'Protein Ontology',
  NCBITAXON: 'NCBI Taxonomy',
  TAXON: 'NCBI Taxonomy',
};

/**
 * Expand an ontology acronym into "Full Name (ACRONYM)" for display.
 *
 * - Exact (case-insensitive) acronym match -> "Full Name (ACRONYM)",
 *   preserving the acronym exactly as the user wrote it.
 * - Anything else (ordinary theory names, already-expanded names) is
 *   returned unchanged.
 */
export function expandOntologyName(name: string): string {
  if (!name) return name;
  const key = name.trim().toUpperCase();
  const full = ONTOLOGY_FULL_NAMES[key];
  if (!full) return name;
  return `${full} (${name.trim()})`;
}

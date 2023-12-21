#!/usr/bin/env python
"""Looks for the given primer sequences and clips matching SFF reads.

Takes eight command line options, input read filename, input read format,
input primer FASTA filename, type of primers (forward, reverse or reverse-
complement), number of mismatches (currently only 0, 1 and 2 are supported),
minimum length to keep a read (after primer trimming), should primer-less
reads be kept (boolean), and finally the output sequence filename.

Both the primer and read sequences can contain IUPAC ambiguity codes like N.

This supports FASTA, FASTQ and SFF sequence files. Colorspace reads are not
supported.

The mismatch parameter does not consider gapped alignemnts, however the
special case of missing bases at the very start or end of the read is handled.
e.g. a primer sequence CCGACTCGAG will match a read starting CGACTCGAG...
if one or more mismatches are allowed.

This can also be used for stripping off (and optionally filtering on) barcodes.

Note that only the trim/clip values in the SFF file are changed, not the flow
information of the full read sequence.

This script is copyright 2011-2013 by Peter Cock, The James Hutton Institute
(formerly the Scottish Crop Research Institute, SCRI), UK. All rights reserved.
See accompanying text file for licence details (MIT/BSD style).

NOTE: Currently it uses Python's regular expression engine for finding the
primers, which for my needs is fast enough.
"""

import re
import sys

if "-v" in sys.argv or "--version" in sys.argv:
    print("v0.0.17")
    sys.exit(0)

from galaxy_utils.sequence.fasta import fastaReader, fastaWriter
from galaxy_utils.sequence.fastq import fastqReader, fastqWriter

try:
    from Bio.Seq import reverse_complement
    from Bio.SeqIO.SffIO import SffIterator, SffWriter
except ImportError:
    sys.exit("Requires Biopython 1.54 or later")
try:
    from Bio.SeqIO.SffIO import ReadRocheXmlManifest
except ImportError:
    # Prior to Biopython 1.56 this was a private function
    from Bio.SeqIO.SffIO import _sff_read_roche_index_xml as ReadRocheXmlManifest

# Parse Command Line
try:
    (
        in_file,
        seq_format,
        primer_fasta,
        primer_type,
        mm,
        min_len,
        keep_negatives,
        out_file,
    ) = sys.argv[1:]
except ValueError:
    sys.exit(
        "Expected 8 arguments, got %i:\n%s" % (len(sys.argv) - 1, " ".join(sys.argv))
    )

if in_file == primer_fasta:
    sys.exit("Same file given as both primer sequences and sequences to clip!")
if in_file == out_file:
    sys.exit("Same file given as both sequences to clip and output!")
if primer_fasta == out_file:
    sys.exit("Same file given as both primer sequences and output!")

try:
    mm = int(mm)
except ValueError:
    sys.exit(
        "Expected non-negative integer number of mismatches (e.g. 0 or 1), not %r" % mm
    )
if mm < 0:
    sys.exit(
        "Expected non-negtive integer number of mismatches (e.g. 0 or 1), not %r" % mm
    )
if mm not in [0, 1, 2]:
    raise NotImplementedError

try:
    min_len = int(min_len)
except ValueError:
    sys.exit("Expected non-negative integer min_len (e.g. 0 or 1), not %r" % min_len)
if min_len < 0:
    sys.exit("Expected non-negtive integer min_len (e.g. 0 or 1), not %r" % min_len)


if keep_negatives.lower() in ["true", "yes", "on"]:
    keep_negatives = True
elif keep_negatives.lower() in ["false", "no", "off"]:
    keep_negatives = False
else:
    sys.exit(
        "Expected boolean for keep_negatives (e.g. true or false), not %r"
        % keep_negatives
    )


if primer_type.lower() == "forward":
    forward = True
    rc = False
elif primer_type.lower() == "reverse":
    forward = False
    rc = False
elif primer_type.lower() == "reverse-complement":
    forward = False
    rc = True
else:
    sys.exit("Expected foward, reverse or reverse-complement not %r" % primer_type)


ambiguous_dna_values = {
    "A": "A",
    "C": "C",
    "G": "G",
    "T": "T",
    "M": "ACM",
    "R": "AGR",
    "W": "ATW",
    "S": "CGS",
    "Y": "CTY",
    "K": "GTK",
    "V": "ACGMRSV",
    "H": "ACTMWYH",
    "D": "AGTRWKD",
    "B": "CGTSYKB",
    "X": ".",  # faster than [GATCMRWSYKVVHDBXN] or even [GATC]
    "N": ".",
}

ambiguous_dna_re = {}
for letter, values in ambiguous_dna_values.items():
    if len(values) == 1:
        ambiguous_dna_re[letter] = values
    else:
        ambiguous_dna_re[letter] = "[%s]" % values


def make_reg_ex(seq):
    """Make regular expression for ambiguous DNA."""
    return "".join(ambiguous_dna_re[letter] for letter in seq)


def make_reg_ex_mm(seq, mm):
    """Make regular expression for mis-matches."""
    if mm > 2:
        raise NotImplementedError("At most 2 mismatches allowed!")
    seq = seq.upper()
    yield make_reg_ex(seq)
    for i in range(1, mm + 1):
        # Missing first/last i bases at very start/end of sequence
        for reg in make_reg_ex_mm(seq[i:], mm - i):
            yield "^" + reg
        for reg in make_reg_ex_mm(seq[:-i], mm - i):
            yield "$" + reg
    if mm >= 1:
        for i, letter in enumerate(seq):
            # We'll use a set to remove any duplicate patterns
            # if letter not in "NX":
            pattern = seq[:i] + "N" + seq[i + 1 :]
            assert len(pattern) == len(seq), "Len %s is %i, len %s is %i" % (
                pattern,
                len(pattern),
                seq,
                len(seq),
            )
            yield make_reg_ex(pattern)
    if mm >= 2:
        for i, letter in enumerate(seq):
            # We'll use a set to remove any duplicate patterns
            # if letter not in "NX":
            for k, letter in enumerate(seq[i + 1 :]):
                # We'll use a set to remove any duplicate patterns
                # if letter not in "NX":
                pattern = (
                    seq[:i] + "N" + seq[i + 1 : i + 1 + k] + "N" + seq[i + k + 2 :]
                )
                assert len(pattern) == len(seq), "Len %s is %i, len %s is %i" % (
                    pattern,
                    len(pattern),
                    seq,
                    len(seq),
                )
                yield make_reg_ex(pattern)


def load_primers_as_re(primer_fasta, mm, rc=False):
    """Load primers as regular expressions.

    Read primer file and record all specified sequences.
    """
    primers = set()
    in_handle = open(primer_fasta)
    reader = fastaReader(in_handle)
    count = 0
    for record in reader:
        if rc:
            seq = reverse_complement(record.sequence)
        else:
            seq = record.sequence
        # primers.add(re.compile(make_reg_ex(seq)))
        count += 1
        for pattern in make_reg_ex_mm(seq, mm):
            primers.add(pattern)
    in_handle.close()
    # Use set to avoid duplicates, sort to have longest first
    # (so more specific primers found before less specific ones)
    primers = sorted(set(primers), key=lambda p: -len(p))
    return count, re.compile("|".join(primers))  # make one monster re!


# Read primer file and record all specified sequences
count, primer = load_primers_as_re(primer_fasta, mm, rc)
print("%i primer sequences" % count)

short_neg = 0
short_clipped = 0
clipped = 0
negs = 0

if seq_format.lower() == "sff":
    # SFF is different because we just change the trim points
    if forward:

        def process(records):
            global short_clipped, short_neg, clipped, negs
            for record in records:
                left_clip = record.annotations["clip_qual_left"]
                right_clip = record.annotations["clip_qual_right"]
                seq = str(record.seq)[left_clip:right_clip].upper()
                result = primer.search(seq)
                if result:
                    # Forward primer, take everything after it
                    # so move the left clip along
                    if len(seq) - result.end() >= min_len:
                        record.annotations["clip_qual_left"] = left_clip + result.end()
                        clipped += 1
                        yield record
                    else:
                        short_clipped += 1
                elif keep_negatives:
                    if len(seq) >= min_len:
                        negs += 1
                        yield record
                    else:
                        short_neg += 1

    else:

        def process(records):
            global short_clipped, short_neg, clipped, negs
            for record in records:
                left_clip = record.annotations["clip_qual_left"]
                right_clip = record.annotations["clip_qual_right"]
                seq = str(record.seq)[left_clip:right_clip].upper()
                result = primer.search(seq)
                if result:
                    # Reverse primer, take everything before it
                    # so move the right clip back
                    new_len = result.start()
                    if new_len >= min_len:
                        record.annotations["clip_qual_right"] = left_clip + new_len
                        clipped += 1
                        yield record
                    else:
                        short_clipped += 1
                elif keep_negatives:
                    if len(seq) >= min_len:
                        negs += 1
                        yield record
                    else:
                        short_neg += 1

    in_handle = open(in_file, "rb")
    try:
        manifest = ReadRocheXmlManifest(in_handle)
    except ValueError:
        manifest = None
    in_handle.seek(0)
    out_handle = open(out_file, "wb")
    writer = SffWriter(out_handle, xml=manifest)
    writer.write_file(process(SffIterator(in_handle)))
    # End of SFF code
elif seq_format.lower().startswith("fastq"):
    in_handle = open(in_file)
    out_handle = open(out_file, "w")
    reader = fastqReader(in_handle)
    writer = fastqWriter(out_handle)
    if forward:
        for record in reader:
            seq = record.sequence.upper()
            result = primer.search(seq)
            if result:
                # Forward primer, take everything after it
                cut = result.end()
                record.sequence = seq[cut:]
                if len(record.sequence) >= min_len:
                    record.quality = record.quality[cut:]
                    clipped += 1
                    writer.write(record)
                else:
                    short_clipped += 1
            elif keep_negatives:
                if len(record) >= min_len:
                    negs += 1
                    writer.write(record)
                else:
                    short_neg += 1
    else:
        for record in reader:
            seq = record.sequence.upper()
            result = primer.search(seq)
            if result:
                # Reverse primer, take everything before it
                cut = result.start()
                record.sequence = seq[:cut]
                if len(record.sequence) >= min_len:
                    record.quality = record.quality[:cut]
                    clipped += 1
                    writer.write(record)
                else:
                    short_clipped += 1
            elif keep_negatives:
                if len(record) >= min_len:
                    negs += 1
                    writer.write(record)
                else:
                    short_neg += 1
elif seq_format.lower() == "fasta":
    in_handle = open(in_file)
    out_handle = open(out_file, "w")
    reader = fastaReader(in_handle)
    writer = fastaWriter(out_handle)
    # Following code is identical to that for FASTQ but without editing qualities
    if forward:
        for record in reader:
            seq = record.sequence.upper()
            result = primer.search(seq)
            if result:
                # Forward primer, take everything after it
                cut = result.end()
                record.sequence = seq[cut:]
                if len(record.sequence) >= min_len:
                    clipped += 1
                    writer.write(record)
                else:
                    short_clipped += 1
            elif keep_negatives:
                if len(record) >= min_len:
                    negs += 1
                    writer.write(record)
                else:
                    short_neg += 1
    else:
        for record in reader:
            seq = record.sequence.upper()
            result = primer.search(seq)
            if result:
                # Reverse primer, take everything before it
                cut = result.start()
                record.sequence = seq[:cut]
                if len(record.sequence) >= min_len:
                    clipped += 1
                    writer.write(record)
                else:
                    short_clipped += 1
            elif keep_negatives:
                if len(record) >= min_len:
                    negs += 1
                    writer.write(record)
                else:
                    short_neg += 1
else:
    sys.exit("Unsupported file type %r" % seq_format)
in_handle.close()
out_handle.close()

print("Kept %i clipped reads," % clipped)
print("discarded %i short." % short_clipped)
if keep_negatives:
    print("Kept %i non-matching reads," % negs)
    print("discarded %i short." % short_neg)

//! Overlapping integer intervals.

/// Merge overlapping `(start, end)` intervals, given in any order.
///
/// The result is sorted by start; touching intervals (`(1, 3)` and `(3, 5)`) merge.
pub fn merge_intervals(mut intervals: Vec<(u32, u32)>) -> Vec<(u32, u32)> {
    intervals.sort_unstable();
    let mut merged: Vec<(u32, u32)> = Vec::with_capacity(intervals.len());
    for (start, end) in intervals {
        match merged.last_mut() {
            Some(last) if start <= last.1 => last.1 = last.1.max(end),
            _ => merged.push((start, end)),
        }
    }
    merged
}

/// The largest item of `items`, or `None` when it is empty (generic: one copy per type used).
pub fn largest<T: PartialOrd + Copy>(items: &[T]) -> Option<T> {
    let mut best = *items.first()?;
    for &item in items {
        if item > best {
            best = item;
        }
    }
    Some(best)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn merges_overlaps_in_any_order() {
        assert_eq!(merge_intervals(vec![(5, 8), (1, 3), (2, 4)]), vec![(1, 4), (5, 8)]);
    }

    #[test]
    fn largest_of_floats_and_ints() {
        assert_eq!(largest(&[1.5, 3.25, 2.0]), Some(3.25));
        assert_eq!(largest::<i32>(&[]), None);
    }
}

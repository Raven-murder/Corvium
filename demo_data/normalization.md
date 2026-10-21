# Normalization: designing dependable relations

Normalization organizes relational tables so that each fact has a clear home. Its practical purpose is to reduce avoidable repetition and the anomalies that repetition creates. A functional dependency X -> Y means that any two rows agreeing on X must also agree on Y. Dependencies describe rules of the application, rather than patterns that happen to appear in a small sample of rows.

## Start with a concrete example

Suppose a college records enrollment in a relation containing StudentID, StudentName, CourseID, CourseTitle, InstructorID, InstructorOffice, and Grade. Assume each course has one instructor, each instructor has one office, and a student receives one grade per course. The candidate key is the pair (StudentID, CourseID). StudentID determines StudentName. CourseID determines CourseTitle and InstructorID. InstructorID determines InstructorOffice. The full pair determines Grade.

This design repeats a student's name for every enrolled course and repeats course information for every enrolled student. Changing an instructor's office requires updating several rows. Missing one row creates an update anomaly. A new course cannot be recorded until someone enrolls, creating an insertion anomaly. Deleting the last enrollment may remove the only record of the course, creating a deletion anomaly.

## First and second normal forms

First normal form requires that each attribute value be atomic for the intended relational model, with no repeating groups. A field containing a comma-separated list of enrolled courses makes individual enrollment facts difficult to constrain and query. Represent each enrollment as its own row instead. Atomicity depends on how the application uses the value; an address need not always be split into every possible component.

Second normal form requires first normal form and no partial dependency of a non-prime attribute on a candidate key. A non-prime attribute belongs to no candidate key. In the example, StudentName depends on only StudentID, and CourseTitle depends on only CourseID. Separate Students(StudentID, StudentName), Courses(CourseID, CourseTitle, InstructorID, InstructorOffice), and Enrollments(StudentID, CourseID, Grade). Each table now describes a more focused kind of fact.

## Third normal form and decomposition checks

Third normal form requires that every nontrivial dependency X -> A have either X as a superkey or A as a prime attribute. In Courses, InstructorID determines InstructorOffice, but InstructorID need not identify a course. Move office information into Instructors(InstructorID, InstructorOffice), leaving the instructor identifier in Courses as a foreign key. This removes the office's transitive dependency on CourseID.

A decomposition should be lossless: joining the new relations must reconstruct valid original rows without inventing combinations. Dependency preservation is also desirable because constraints can then be checked without expensive joins. Foreign keys preserve references, while primary keys and unique constraints enforce identifiers. Normalization does not automatically guarantee faster reads; extra joins may add work. Deliberate denormalization can support measured performance needs, but it requires a plan to keep duplicated facts consistent.

For revision, write the candidate keys first, list dependencies from stated rules, identify the offending dependency, and explain the anomaly removed by each split. Do not infer keys merely because today's sample happens to contain unique values.
